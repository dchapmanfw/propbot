"""Discord bot entry point with background expiry handling."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from bets import BetService, DurationParseError, build_bet_embed, ensure_yes_no_reactions, parse_duration
from commands import PropBetCommands
from config import (
    BET_EXPIRY_CHECK_INTERVAL,
    DEV_GUILD_ID,
    DISCORD_TOKEN,
    MARKET_BOARD_CHANNEL_ID,
    MARKET_BOARD_RETENTION,
    NO_EMOJI,
    UNRESOLVED_REFUND_AFTER,
    YES_EMOJI,
)
from database import Database
from market_charts import build_market_board_embeds, build_market_board_list_embed
from markets import MarketService, build_market_embed
from models import Bet, BetKind, BetStatus
from process_guard import kill_other_bot_instances

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Space board message edits so large chart embeds do not trip channel 429s.
_BOARD_EDIT_SPACING_SECONDS = 1.25

INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True
INTENTS.reactions = True


class PropBetBot(commands.Bot):
    """Custom bot that wires database, services, and open-bet tracking."""

    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=INTENTS)
        self.db = Database()
        self._open_bet_ids: set[int] = set()
        self._wager_prompt_at: dict[tuple[int, int], float] = {}
        self._board_refresh_locks: dict[int, asyncio.Lock] = {}
        self._board_refresh_pending: set[int] = set()
        try:
            self._unresolved_refund_after = parse_duration(UNRESOLVED_REFUND_AFTER)
        except DurationParseError as exc:
            raise SystemExit(
                f"Invalid UNRESOLVED_REFUND_AFTER ({UNRESOLVED_REFUND_AFTER!r}): {exc}"
            ) from exc
        try:
            self._market_board_retention = parse_duration(MARKET_BOARD_RETENTION)
        except DurationParseError as exc:
            raise SystemExit(
                f"Invalid MARKET_BOARD_RETENTION ({MARKET_BOARD_RETENTION!r}): {exc}"
            ) from exc

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.add_cog(PropBetCommands(self))

        await self._sync_slash_commands()

        # Resume tracking open bets after restart.
        open_bets = await self.db.get_open_bets()
        for bet in open_bets:
            self.track_open_bet(bet)
        logger.info("Tracking %d open bet(s) after startup", len(open_bets))
        await self._restore_open_bet_reactions(open_bets)

        if not self.check_expired_bets.is_running():
            self.check_expired_bets.start()

        if MARKET_BOARD_CHANNEL_ID is not None:
            from datetime import datetime, timezone

            cutoff = datetime.now(timezone.utc) - self._market_board_retention
            for guild_id in await self.db.get_guild_ids_with_board_markets(cutoff):
                await self.refresh_market_board(guild_id)

    async def _restore_open_bet_reactions(self, open_bets: list[Bet]) -> None:
        """Re-add YES/NO reactions on open bet messages (e.g. after thread creation)."""
        restored = 0
        for bet in open_bets:
            if not bet.message_id:
                continue
            channel = await self.fetch_channel(bet.channel_id)
            if not channel:
                continue
            try:
                message = await channel.fetch_message(bet.message_id)
            except discord.NotFound:
                continue
            before = {str(r.emoji) for r in message.reactions}
            await ensure_yes_no_reactions(message)
            after = before | {YES_EMOJI, NO_EMOJI}
            if after != before:
                restored += 1
        if restored:
            logger.info("Restored YES/NO reactions on %d open bet message(s)", restored)

    async def _sync_slash_commands(self) -> None:
        """Register slash commands with Discord (guild sync is instant for dev)."""
        if DEV_GUILD_ID is not None:
            guild = discord.Object(id=DEV_GUILD_ID)
            # Cog commands live on the global tree — copy them before guild sync.
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            names = sorted(cmd.name for cmd in synced)
            logger.info(
                "Slash commands synced to guild %s (%d): %s",
                DEV_GUILD_ID,
                len(names),
                ", ".join(names),
            )
            return

        synced = await self.tree.sync()
        names = sorted(cmd.name for cmd in synced)
        logger.info("Slash commands synced globally (%d): %s", len(names), ", ".join(names))

    async def close(self) -> None:
        self.check_expired_bets.cancel()
        await self.db.close()
        await super().close()

    def track_open_bet(self, bet: Bet) -> None:
        self._open_bet_ids.add(bet.id)

    def untrack_bet(self, bet_id: int) -> None:
        self._open_bet_ids.discard(bet_id)

    async def fetch_channel(
        self, channel_id: int
    ) -> discord.abc.Messageable | None:
        """Return a channel from cache, fetching from the API if needed."""
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            fetched = await super().fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.warning("Could not fetch channel %s: %s", channel_id, exc)
            return None
        if isinstance(fetched, discord.abc.Messageable):
            return fetched
        return None

    def _board_lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._board_refresh_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._board_refresh_locks[guild_id] = lock
        return lock

    async def refresh_bet_message(
        self,
        bet_id: int,
        *,
        footer_extra: str | None = None,
        update_board: bool = True,
    ) -> None:
        """Update the public embed for a bet message."""
        bet = await self.db.get_bet(bet_id)
        if not bet or not bet.message_id:
            return

        channel = await self.fetch_channel(bet.channel_id)
        if not channel:
            return

        try:
            message = await channel.fetch_message(bet.message_id)
        except discord.NotFound:
            return

        guild = channel.guild if hasattr(channel, "guild") else None
        creator = guild.get_member(bet.creator_id) if guild else None

        if bet.bet_kind == BetKind.MARKET:
            positions = await self.db.get_market_positions_for_bet(bet_id)
            embed = build_market_embed(
                bet,
                creator=creator,
                positions=positions,
                footer_extra=footer_extra,
            )
        else:
            wagers = await self.db.get_wagers_for_bet(bet_id)
            bookie_balance = None
            if bet.status == BetStatus.OPEN:
                bookie_balance = await self.db.get_balance(
                    bet.guild_id, bet.creator_id
                )
            embed = build_bet_embed(
                bet,
                creator=creator,
                wagers=wagers,
                footer_extra=footer_extra,
                bookie_balance=bookie_balance,
            )
        await message.edit(embed=embed)
        if bet.status == BetStatus.OPEN:
            await ensure_yes_no_reactions(message)

        if update_board and bet.bet_kind == BetKind.MARKET:
            await self.refresh_market_board_for_bet(bet_id)

    async def refresh_market_board_for_bet(self, bet_id: int) -> None:
        """Refresh the guild board after a change to one market."""
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.bet_kind != BetKind.MARKET:
            return
        await self.refresh_market_board(bet.guild_id)

    async def _delete_board_message(
        self,
        channel: discord.abc.Messageable,
        message_id: int,
        guild_id: int,
    ) -> None:
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            logger.warning(
                "Could not delete market board message %s for guild %s: %s",
                message_id,
                guild_id,
                exc,
            )

    async def refresh_market_board(self, guild_id: int) -> None:
        """Post or update market board message(s); coalesce overlapping refreshes."""
        if MARKET_BOARD_CHANNEL_ID is None:
            return

        self._board_refresh_pending.add(guild_id)
        async with self._board_lock_for(guild_id):
            if guild_id not in self._board_refresh_pending:
                return
            while guild_id in self._board_refresh_pending:
                self._board_refresh_pending.discard(guild_id)
                await self._refresh_market_board_unlocked(guild_id)

    async def _refresh_market_board_unlocked(self, guild_id: int) -> None:
        """Post or update market board message(s) for a guild; drop stale pages."""
        from datetime import datetime, timezone

        channel = await self.fetch_channel(MARKET_BOARD_CHANNEL_ID)
        if not channel:
            return

        cutoff = datetime.now(timezone.utc) - self._market_board_retention
        markets = await self.db.get_markets_for_board(guild_id, cutoff)
        entries: list[tuple] = []
        for market in markets:
            snapshots = await self.db.get_market_snapshots(market.id)
            entries.append((market, snapshots))

        embeds = build_market_board_embeds(entries, guild_id=guild_id)
        list_embed = build_market_board_list_embed(entries, guild_id=guild_id)
        existing_ids = await self.db.get_market_board_message_ids(guild_id)
        list_message_id = await self.db.get_market_board_list_message_id(guild_id)

        if not embeds:
            if list_message_id:
                await self._delete_board_message(channel, list_message_id, guild_id)
                await self.db.clear_market_board_list_message_id(guild_id)
            for message_id in existing_ids:
                await self._delete_board_message(channel, message_id, guild_id)
            await self.db.clear_market_board_message_ids(guild_id)
            return

        list_message = await self._upsert_board_message(
            channel,
            list_message_id,
            list_embed,
            guild_id=guild_id,
        )
        await self.db.set_market_board_list_message_id(guild_id, list_message.id)

        new_ids: list[int] = []
        for page_index, embed in enumerate(embeds):
            await asyncio.sleep(_BOARD_EDIT_SPACING_SECONDS)
            message_id = (
                existing_ids[page_index] if page_index < len(existing_ids) else None
            )
            message = await self._upsert_board_message(
                channel,
                message_id,
                embed,
                guild_id=guild_id,
            )
            new_ids.append(message.id)

        for message_id in existing_ids[len(embeds) :]:
            await asyncio.sleep(_BOARD_EDIT_SPACING_SECONDS)
            await self._delete_board_message(channel, message_id, guild_id)

        await self.db.set_market_board_message_ids(guild_id, new_ids)

    async def _upsert_board_message(
        self,
        channel: discord.abc.Messageable,
        message_id: int | None,
        embed: discord.Embed,
        *,
        guild_id: int,
    ) -> discord.Message:
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                return await channel.send(embed=embed)
            await message.edit(embed=embed)
            return message
        return await channel.send(embed=embed)

    async def _purge_stale_market_board_messages(
        self, *, extra_guild_ids: set[int] | None = None
    ) -> None:
        if MARKET_BOARD_CHANNEL_ID is None:
            return

        from datetime import datetime, timezone

        purge_before = datetime.now(timezone.utc) - self._market_board_retention
        guild_ids = set(await self.db.get_guild_ids_needing_board_refresh(purge_before))
        if extra_guild_ids:
            guild_ids |= extra_guild_ids
        for guild_id in sorted(guild_ids):
            await self.refresh_market_board(guild_id)

    @tasks.loop(seconds=BET_EXPIRY_CHECK_INTERVAL)
    async def check_expired_bets(self) -> None:
        """Close expired open bets and refund stale unresolved closed bets."""
        bet_service = BetService(self.db)
        market_service = MarketService(self.db)
        board_guild_ids: set[int] = set()

        for bet in await self.db.get_expired_open_bets():
            if bet.bet_kind == BetKind.MARKET:
                closed = await market_service.close_market(bet.id)
            else:
                closed = await bet_service.close_bet(bet.id)
            if closed:
                logger.info("Closed expired %s #%d", bet.bet_kind.value, bet.id)
                await self.refresh_bet_message(bet.id, update_board=False)
                if bet.bet_kind == BetKind.MARKET:
                    board_guild_ids.add(bet.guild_id)
                self.untrack_bet(bet.id)

        for bet in await self.db.get_stale_closed_bets(self._unresolved_refund_after):
            if bet.bet_kind == BetKind.MARKET:
                result = await market_service.refund_unresolved_market(bet.id)
            else:
                result = await bet_service.refund_unresolved_bet(bet.id)
            if result:
                refunded_bet, count = result
                logger.info(
                    "Auto-refunded %s #%d (%d position(s)/wager(s)) — unresolved past grace period",
                    bet.bet_kind.value,
                    bet.id,
                    count,
                )
                await self.refresh_bet_message(
                    bet.id,
                    footer_extra="Auto-refunded — never resolved",
                    update_board=False,
                )
                if bet.bet_kind == BetKind.MARKET:
                    board_guild_ids.add(bet.guild_id)
                self.untrack_bet(bet.id)

        await self._purge_stale_market_board_messages(extra_guild_ids=board_guild_ids)

    @check_expired_bets.before_loop
    async def before_check_expired_bets(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
        )

    kill_other_bot_instances()

    bot = PropBetBot()

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

"""Discord bot entry point with background expiry handling."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from bets import BetService, build_bet_embed
from commands import PropBetCommands
from config import BET_EXPIRY_CHECK_INTERVAL, DISCORD_TOKEN
from database import Database
from models import Bet, BetStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True
INTENTS.reactions = True


class PropBetBot(commands.Bot):
    """Custom bot that wires database, services, and open-bet tracking."""

    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=INTENTS)
        self.db = Database()
        self._open_bet_ids: set[int] = set()

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.add_cog(PropBetCommands(self))

        # Sync slash commands globally (or use guild= for faster dev iteration).
        await self.tree.sync()
        logger.info("Slash commands synced")

        # Resume tracking open bets after restart.
        open_bets = await self.db.get_open_bets()
        for bet in open_bets:
            self.track_open_bet(bet)
        logger.info("Tracking %d open bet(s) after startup", len(open_bets))

        if not self.check_expired_bets.is_running():
            self.check_expired_bets.start()

    async def close(self) -> None:
        self.check_expired_bets.cancel()
        await self.db.close()
        await super().close()

    def track_open_bet(self, bet: Bet) -> None:
        self._open_bet_ids.add(bet.id)

    def untrack_bet(self, bet_id: int) -> None:
        self._open_bet_ids.discard(bet_id)

    async def refresh_bet_message(self, bet_id: int) -> None:
        """Update the public embed for a bet message."""
        bet = await self.db.get_bet(bet_id)
        if not bet or not bet.message_id:
            return

        channel = self.get_channel(bet.channel_id)
        if not channel:
            return

        try:
            message = await channel.fetch_message(bet.message_id)
        except discord.NotFound:
            return

        guild = channel.guild if hasattr(channel, "guild") else None
        creator = guild.get_member(bet.creator_id) if guild else None
        wagers = await self.db.get_wagers_for_bet(bet_id)
        embed = build_bet_embed(bet, creator=creator, wagers=wagers)
        await message.edit(embed=embed)

    @tasks.loop(seconds=BET_EXPIRY_CHECK_INTERVAL)
    async def check_expired_bets(self) -> None:
        """Close bets whose betting window has ended."""
        expired = await self.db.get_expired_open_bets()
        service = BetService(self.db)

        for bet in expired:
            closed = await service.close_bet(bet.id)
            if closed:
                logger.info("Closed expired bet #%d", bet.id)
                await self.refresh_bet_message(bet.id)
                self.untrack_bet(bet.id)

    @check_expired_bets.before_loop
    async def before_check_expired_bets(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
        )

    bot = PropBetBot()

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

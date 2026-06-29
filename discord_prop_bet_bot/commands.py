"""Slash commands, modals, and reaction handlers for the prop bet bot."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bets import (
    BetService,
    DurationParseError,
    build_bet_embed,
    build_help_embed,
    build_leaderboard_description,
    compute_max_wager,
    emoji_from_pick,
    format_balance_message,
    parse_duration,
    pick_from_emoji,
)
from channel_policy import allowed_channel_message, is_allowed_channel
from config import ALLOWED_CHANNEL_ID, NO_EMOJI, YES_EMOJI
from database import Database
from economy import EconomyService, REDEMPTION_COST
from models import BetOutcome, BetStatus, WagerPick

if TYPE_CHECKING:
    from bot import PropBetBot

logger = logging.getLogger(__name__)

# Seconds before re-prompting the same user on the same bet after a reaction.
WAGER_PROMPT_COOLDOWN = 120


class WagerModal(discord.ui.Modal, title="Enter your wager"):
    """Modal prompting the user for a wager amount after reacting to a bet."""

    amount_input = discord.ui.TextInput(
        label="Wager amount",
        placeholder="Enter a positive whole number",
        min_length=1,
        max_length=10,
    )

    def __init__(
        self,
        bot: PropBetBot,
        bet_id: int,
        pick: WagerPick,
        guild_id: int,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.bet_id = bet_id
        self.pick = pick
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = int(self.amount_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Please enter a valid positive integer.", ephemeral=True
            )
            return

        service = BetService(self.bot.db)
        try:
            wager, balance = await service.place_or_update_wager(
                self.guild_id,
                self.bet_id,
                interaction.user.id,
                self.pick,
                amount,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"You wagered **{wager.amount}** on **{self.pick.value.upper()}** "
            f"for bet #{self.bet_id}. Remaining balance: **{balance}**.",
            ephemeral=True,
        )

        await self.bot.refresh_bet_message(self.bet_id)
        self.bot._wager_prompt_at.pop((interaction.user.id, self.bet_id), None)


class WagerButtonView(discord.ui.View):
    """Button that opens the wager modal (used after reaction or in DMs)."""

    def __init__(
        self,
        bot: PropBetBot,
        bet_id: int,
        pick: WagerPick,
        guild_id: int,
        owner_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.bet_id = bet_id
        self.pick = pick
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This button is not for you.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Enter wager", style=discord.ButtonStyle.primary)
    async def enter_wager(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            WagerModal(self.bot, self.bet_id, self.pick, self.guild_id)
        )


class PropBetCommands(commands.Cog):
    """Slash commands and event handlers for prop bets."""

    def __init__(self, bot: PropBetBot) -> None:
        self.bot = bot
        self.db: Database = bot.db
        self._reactions_reconciled = False

    async def _notify_user(
        self,
        user: discord.User | discord.Member,
        channel_id: int,
        content: str,
        *,
        view: discord.ui.View | None = None,
        delete_after: float | None = None,
        prefer_channel: bool = False,
    ) -> None:
        """Deliver a message via DM, falling back to the bet channel on failure."""
        channel = await self.bot.fetch_channel(channel_id)
        channel_content = f"{user.mention} {content}"

        if prefer_channel and channel is not None:
            try:
                await channel.send(
                    channel_content, view=view, delete_after=delete_after
                )
            except discord.HTTPException as exc:
                logger.warning(
                    "Could not notify user %s in channel %s: %s",
                    user.id,
                    channel_id,
                    exc,
                )
            return

        try:
            await user.send(content, view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info("DM to %s failed (%s), using channel fallback", user.id, exc)
            if channel is None:
                logger.warning(
                    "Could not notify user %s: DM failed and channel %s unavailable",
                    user.id,
                    channel_id,
                )
                return
            try:
                await channel.send(
                    channel_content,
                    view=view,
                    delete_after=delete_after or 120,
                )
            except discord.HTTPException as send_exc:
                logger.warning(
                    "Channel fallback notify failed for user %s: %s",
                    user.id,
                    send_exc,
                )

    def _should_prompt_wager(self, user_id: int, bet_id: int) -> bool:
        key = (user_id, bet_id)
        last = self.bot._wager_prompt_at.get(key)
        if last is not None and time.monotonic() - last < WAGER_PROMPT_COOLDOWN:
            return False
        self.bot._wager_prompt_at[key] = time.monotonic()
        return True

    async def _maybe_prompt_wager_for_reaction(
        self,
        bet,
        user_id: int,
        pick: WagerPick,
        channel_id: int,
    ) -> None:
        """DM (or channel-fallback) a user to enter a wager after they react."""
        if user_id == self.bot.user.id:
            return

        if datetime.now(timezone.utc) >= bet.close_time:
            await BetService(self.db).close_bet(bet.id)
            await self.bot.refresh_bet_message(bet.id)
            return

        if user_id == bet.creator_id:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await self._notify_user(
                user,
                channel_id,
                "You cannot wager on a bet you created.",
                delete_after=15,
                prefer_channel=True,
            )
            return

        existing = await self.db.get_wager(bet.id, user_id)
        if existing and existing.pick == pick:
            return

        if not self._should_prompt_wager(user_id, bet.id):
            return

        await self.db.ensure_user(bet.guild_id, user_id)
        balance = await self.db.get_balance(bet.guild_id, user_id) or 0
        existing_amount = existing.amount if existing else 0
        max_wager = compute_max_wager(balance, existing_amount)

        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        view = WagerButtonView(self.bot, bet.id, pick, bet.guild_id, owner_id=user_id)
        prompt = (
            f"You're joining bet **#{bet.id}** with **{pick.value.upper()}**.\n"
            f"Max wager: **{max_wager}** coins.\n"
            f"Click **Enter wager** to set your amount."
        )
        await self._notify_user(
            user,
            channel_id,
            prompt,
            view=view,
            delete_after=120,
        )

    async def _reconcile_open_bet_reactions(self) -> None:
        """Prompt users who reacted while the bot was offline."""
        open_bets = await self.db.get_open_bets()
        for bet in open_bets:
            if not bet.message_id:
                continue
            channel = await self.bot.fetch_channel(bet.channel_id)
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(bet.message_id)
            except discord.NotFound:
                logger.warning(
                    "Open bet #%d message %s not found during reaction reconcile",
                    bet.id,
                    bet.message_id,
                )
                continue
            except discord.Forbidden:
                logger.warning(
                    "Missing access to open bet #%d message during reaction reconcile",
                    bet.id,
                )
                continue

            for reaction in message.reactions:
                pick = pick_from_emoji(str(reaction.emoji))
                if not pick:
                    continue
                async for user in reaction.users():
                    if user.bot or user.id == bet.creator_id:
                        continue
                    await self._maybe_prompt_wager_for_reaction(
                        bet, user.id, pick, bet.channel_id
                    )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._reactions_reconciled:
            return
        self._reactions_reconciled = True
        await self._reconcile_open_bet_reactions()

    async def _is_admin_or_creator(
        self, interaction: discord.Interaction, bet_creator_id: int
    ) -> bool:
        if interaction.user.id == bet_creator_id:
            return True
        if not interaction.guild:
            return False
        perms = interaction.user.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    async def _require_allowed_channel(self, interaction: discord.Interaction) -> bool:
        """Reject slash commands used outside the configured betting channel."""
        if is_allowed_channel(interaction.channel_id):
            return True
        await interaction.response.send_message(
            allowed_channel_message(), ephemeral=True
        )
        return False

    @app_commands.command(name="help", description="How to use the prop bet bot")
    async def help_command(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        embed = build_help_embed()
        if (
            ALLOWED_CHANNEL_ID is not None
            and interaction.channel_id != ALLOWED_CHANNEL_ID
        ):
            embed.add_field(
                name="Wrong channel?",
                value=allowed_channel_message(),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="balance", description="Show your current balance")
    async def balance(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await self._require_allowed_channel(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        user = await self.db.ensure_user(interaction.guild.id, interaction.user.id)
        await interaction.followup.send(format_balance_message(user))

    @app_commands.command(
        name="reset",
        description="Bail out to starting cash (adds anti-prestige on the leaderboard)",
    )
    async def reset_balance(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await self._require_allowed_channel(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        service = EconomyService(self.db)
        try:
            user = await service.reset_balance(
                interaction.guild.id, interaction.user.id
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return

        await interaction.followup.send(
            f"Balance reset to **{user.balance}** coins. "
            f"Anti-prestige is now **↩️×{user.reset_count}**."
        )

    @app_commands.command(
        name="redeem",
        description="Pay 2× starting balance to remove one anti-prestige level",
    )
    async def redeem_reset(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await self._require_allowed_channel(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        service = EconomyService(self.db)
        try:
            user = await service.redeem_reset(
                interaction.guild.id, interaction.user.id
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return

        prestige_note = (
            f"Anti-prestige is now **↩️×{user.reset_count}**."
            if user.reset_count > 0
            else "Your anti-prestige record is clear."
        )
        await interaction.followup.send(
            f"Paid **{REDEMPTION_COST}** coins. {prestige_note} "
            f"Remaining balance: **{user.balance}** coins."
        )

    @app_commands.command(
        name="bet_create",
        description="Create a new yes/no prop bet",
    )
    @app_commands.describe(
        question="The yes/no question to bet on",
        duration="How long betting stays open (e.g. 2h, 30m, 1d)",
        yes_odds="Payout multiplier for YES winners",
        no_odds="Payout multiplier for NO winners",
    )
    async def bet_create(
        self,
        interaction: discord.Interaction,
        question: str,
        duration: str,
        yes_odds: float,
        no_odds: float,
    ) -> None:
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message(
                "This command can only be used in a server channel.", ephemeral=True
            )
            return
        if not await self._require_allowed_channel(interaction):
            return

        if yes_odds <= 0 or no_odds <= 0:
            await interaction.response.send_message(
                "Odds must be positive numbers.", ephemeral=True
            )
            return

        try:
            delta = parse_duration(duration)
        except DurationParseError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer()

        close_time = datetime.now(timezone.utc) + delta
        await self.db.ensure_user(interaction.guild.id, interaction.user.id)

        bet = await self.db.create_bet(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            creator_id=interaction.user.id,
            question=question,
            close_time=close_time,
            yes_odds=yes_odds,
            no_odds=no_odds,
        )

        embed = build_bet_embed(
            bet,
            creator=interaction.user,
            bookie_balance=await self.db.get_balance(
                interaction.guild.id, interaction.user.id
            ),
        )
        await interaction.edit_original_response(embed=embed)
        message = await interaction.original_response()

        await self.db.set_bet_message_id(bet.id, message.id)
        try:
            await message.add_reaction(YES_EMOJI)
            await message.add_reaction(NO_EMOJI)
        except discord.Forbidden:
            logger.warning(
                "Missing channel access to add reactions in channel %s",
                interaction.channel.id,
            )
            try:
                await BetService(self.db).cancel_bet(bet.id)
            except ValueError:
                pass
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            await interaction.followup.send(
                "I posted the bet but **could not add reactions** in this channel "
                f"({interaction.channel.mention}).\n\n"
                "Give my role **View Channel**, **Send Messages**, **Add Reactions**, "
                "and **Read Message History** in this channel, then run `/bet_create` again.",
                ephemeral=True,
            )
            return

        bet = await self.db.get_bet(bet.id)
        assert bet is not None
        self.bot.track_open_bet(bet)

    @app_commands.command(name="bet_resolve", description="Resolve a bet with an outcome")
    @app_commands.describe(
        bet_id="The bet ID to resolve",
        outcome="Final outcome: yes, no, or refund (tie/N/A)",
    )
    @app_commands.choices(
        outcome=[
            app_commands.Choice(name="YES", value="yes"),
            app_commands.Choice(name="NO", value="no"),
            app_commands.Choice(name="Refund (tie / N/A)", value="refund"),
        ]
    )
    async def bet_resolve(
        self,
        interaction: discord.Interaction,
        bet_id: int,
        outcome: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await self._require_allowed_channel(interaction):
            return

        await interaction.response.defer()

        bet = await self.db.get_bet(bet_id)
        if not bet or bet.guild_id != interaction.guild.id:
            await interaction.followup.send("Bet not found.", ephemeral=True)
            return

        if not await self._is_admin_or_creator(interaction, bet.creator_id):
            await interaction.followup.send(
                "Only the bet creator or a server admin can resolve this bet.",
                ephemeral=True,
            )
            return

        if bet.status == BetStatus.OPEN:
            if datetime.now(timezone.utc) >= bet.close_time:
                await BetService(self.db).close_bet(bet_id)
                bet = await self.db.get_bet(bet_id)
                assert bet is not None
            else:
                await interaction.followup.send(
                    "This bet is still open. Wait until betting closes, then resolve.",
                    ephemeral=True,
                )
                return

        service = BetService(self.db)
        try:
            resolved, payouts = await service.resolve_bet(
                bet_id, BetOutcome(outcome.value)
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        wagers = await self.db.get_wagers_for_bet(bet_id)
        creator = interaction.guild.get_member(bet.creator_id)
        embed = build_bet_embed(
            resolved,
            creator=creator,
            wagers=wagers,
            footer_extra="Betting closed",
        )

        lines = []
        for wager, payout in payouts:
            if outcome.value == "refund":
                lines.append(f"<@{wager.user_id}> refunded **{payout}**")
            else:
                lines.append(
                    f"<@{wager.user_id}> won **{payout}** "
                    f"({emoji_from_pick(wager.pick)} {wager.amount} @ "
                    f"{resolved.yes_odds if wager.pick == WagerPick.YES else resolved.no_odds}x)"
                )

        result_text = "\n".join(lines) if lines else "_No winning wagers._"
        await interaction.followup.send(
            content=f"Bet #{bet_id} resolved.\n{result_text}",
            embed=embed,
        )

        message = await self._get_bet_message(resolved)
        if message:
            await message.edit(embed=embed)

        self.bot.untrack_bet(bet_id)

    @app_commands.command(name="bet_cancel", description="Cancel an unresolved bet and refund wagers")
    async def bet_cancel(self, interaction: discord.Interaction, bet_id: int) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await self._require_allowed_channel(interaction):
            return

        await interaction.response.defer()

        bet = await self.db.get_bet(bet_id)
        if not bet or bet.guild_id != interaction.guild.id:
            await interaction.followup.send("Bet not found.", ephemeral=True)
            return

        if not await self._is_admin_or_creator(interaction, bet.creator_id):
            await interaction.followup.send(
                "Only the bet creator or a server admin can cancel this bet.",
                ephemeral=True,
            )
            return

        service = BetService(self.db)
        try:
            wagers = await service.cancel_bet(bet_id)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        bet = await self.db.get_bet(bet_id)
        assert bet is not None
        creator = interaction.guild.get_member(bet.creator_id)
        embed = build_bet_embed(bet, creator=creator, footer_extra="Cancelled — all wagers refunded")

        await interaction.followup.send(
            f"Bet #{bet_id} cancelled. Refunded **{len(wagers)}** wager(s).",
            embed=embed,
        )

        message = await self._get_bet_message(bet)
        if message:
            await message.edit(embed=embed)

        self.bot.untrack_bet(bet_id)

    @app_commands.command(name="bet_status", description="Show bet details and participants")
    async def bet_status(self, interaction: discord.Interaction, bet_id: int) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await self._require_allowed_channel(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        bet = await self.db.get_bet(bet_id)
        if not bet or bet.guild_id != interaction.guild.id:
            await interaction.followup.send("Bet not found.")
            return

        wagers = await self.db.get_wagers_for_bet(bet_id)
        creator = interaction.guild.get_member(bet.creator_id)
        bookie_balance = None
        if bet.status == BetStatus.OPEN:
            bookie_balance = await self.db.get_balance(
                bet.guild_id, bet.creator_id
            )
        embed = build_bet_embed(
            bet,
            creator=creator,
            wagers=wagers,
            bookie_balance=bookie_balance,
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="my_bets", description="Show your active and recent bets")
    async def my_bets(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await self._require_allowed_channel(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        bets = await self.db.get_user_bets(interaction.guild.id, interaction.user.id)
        if not bets:
            await interaction.followup.send(
                "You have no bets in this server yet.",
            )
            return

        lines = []
        for bet in bets:
            wager = await self.db.get_wager(bet.id, interaction.user.id)
            extra = ""
            if wager:
                extra = f" — your wager: {emoji_from_pick(wager.pick)} {wager.amount}"
            elif bet.creator_id == interaction.user.id:
                extra = " — you created this bet"
            lines.append(f"**#{bet.id}** [{bet.status.value}] {bet.question}{extra}")

        embed = discord.Embed(
            title="Your bets",
            description="\n".join(lines[:10]),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="leaderboard", description="Top balances in this server")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await self._require_allowed_channel(interaction):
            return

        await interaction.response.defer()

        rows = await self.db.get_leaderboard(interaction.guild.id, limit=10)
        if not rows:
            await interaction.followup.send(
                "No balances recorded yet. Place a bet to get started!",
            )
            return

        embed = discord.Embed(
            title=f"{interaction.guild.name} Leaderboard",
            description=build_leaderboard_description(rows),
            color=discord.Color.gold(),
        )
        embed.set_footer(
            text="Ranked by prestige (no resets first), then balance. ↩️ = bailout count."
        )
        await interaction.followup.send(embed=embed)

    async def _get_bet_message(self, bet) -> discord.Message | None:
        channel = self.bot.get_channel(bet.channel_id)
        if not channel or not bet.message_id:
            return None
        try:
            return await channel.fetch_message(bet.message_id)
        except discord.NotFound:
            return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.bot.user.id:
            return
        if not is_allowed_channel(payload.channel_id):
            return

        emoji = str(payload.emoji)
        pick = pick_from_emoji(emoji)
        if not pick:
            return

        bet = await self.db.get_bet_by_message(payload.message_id)
        if not bet or bet.status != BetStatus.OPEN:
            return

        await self._maybe_prompt_wager_for_reaction(
            bet, payload.user_id, pick, payload.channel_id
        )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.user_id == self.bot.user.id:
            return
        if not is_allowed_channel(payload.channel_id):
            return

        pick = pick_from_emoji(str(payload.emoji))
        if not pick:
            return

        bet = await self.db.get_bet_by_message(payload.message_id)
        if not bet or bet.status != BetStatus.OPEN:
            return

        existing = await self.db.get_wager(bet.id, payload.user_id)
        if not existing or existing.pick != pick:
            return

        service = BetService(self.db)
        refunded = await service.remove_wager_and_refund(
            bet.guild_id, bet.id, payload.user_id
        )
        if refunded:
            await self.bot.refresh_bet_message(bet.id)
            self.bot._wager_prompt_at.pop((payload.user_id, bet.id), None)
            user = self.bot.get_user(payload.user_id) or await self.bot.fetch_user(
                payload.user_id
            )
            await self._notify_user(
                user,
                payload.channel_id,
                f"Your wager of **{refunded}** on bet #{bet.id} was removed and refunded.",
                delete_after=30,
                prefer_channel=True,
            )


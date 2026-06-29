"""Business logic for prop bets: duration parsing, payouts, embeds."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import discord

from config import NO_EMOJI, YES_EMOJI
from database import Database
from models import Bet, BetOutcome, BetStatus, Wager, WagerPick

# Matches durations like "2h", "30m", "1d", "2 hours", "45 min".
_DURATION_PATTERN = re.compile(
    r"^(\d+)\s*(h(?:ours?)?|m(?:in(?:utes?)?)?|d(?:ays?)?|s(?:ec(?:onds?)?)?)$",
    re.IGNORECASE,
)


class DurationParseError(ValueError):
    """Raised when a duration string cannot be parsed."""


def parse_duration(duration: str) -> timedelta:
    """Parse human-friendly duration strings into a timedelta."""
    text = duration.strip().lower()
    match = _DURATION_PATTERN.match(text)
    if not match:
        raise DurationParseError(
            f"Invalid duration '{duration}'. Use formats like 2h, 30m, 1d, or 45s."
        )

    amount = int(match.group(1))
    if amount <= 0:
        raise DurationParseError("Duration must be a positive number.")

    unit = match.group(2)[0]
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "d":
        return timedelta(days=amount)
    return timedelta(seconds=amount)


def compute_payout(wager: Wager, bet: Bet, outcome: BetOutcome) -> int:
    """Return payout for a winning wager (wager * odds). Losers get 0."""
    if outcome == BetOutcome.REFUND:
        return wager.amount
    if outcome == BetOutcome.YES and wager.pick == WagerPick.YES:
        return int(wager.amount * bet.yes_odds)
    if outcome == BetOutcome.NO and wager.pick == WagerPick.NO:
        return int(wager.amount * bet.no_odds)
    return 0


def pick_from_emoji(emoji: str) -> WagerPick | None:
    if emoji == YES_EMOJI:
        return WagerPick.YES
    if emoji == NO_EMOJI:
        return WagerPick.NO
    return None


def emoji_from_pick(pick: WagerPick) -> str:
    return YES_EMOJI if pick == WagerPick.YES else NO_EMOJI


def status_label(status: BetStatus) -> str:
    return {
        BetStatus.OPEN: "🟢 Open",
        BetStatus.CLOSED: "🔴 Closed (awaiting resolution)",
        BetStatus.RESOLVED: "✅ Resolved",
        BetStatus.CANCELLED: "🚫 Cancelled",
    }[status]


def build_bet_embed(
    bet: Bet,
    creator: discord.abc.User | None = None,
    wagers: list[Wager] | None = None,
    footer_extra: str | None = None,
) -> discord.Embed:
    """Build the public embed shown on bet messages."""
    color = {
        BetStatus.OPEN: discord.Color.green(),
        BetStatus.CLOSED: discord.Color.orange(),
        BetStatus.RESOLVED: discord.Color.blue(),
        BetStatus.CANCELLED: discord.Color.dark_grey(),
    }[bet.status]

    embed = discord.Embed(
        title=f"Prop Bet #{bet.id}",
        description=bet.question,
        color=color,
        timestamp=bet.created_at,
    )

    creator_name = creator.mention if creator else f"<@{bet.creator_id}>"
    embed.add_field(name="Creator", value=creator_name, inline=True)
    embed.add_field(name="Status", value=status_label(bet.status), inline=True)
    embed.add_field(
        name="Closes",
        value=discord.utils.format_dt(bet.close_time, style="R"),
        inline=True,
    )
    embed.add_field(name="YES odds", value=f"{bet.yes_odds:.2f}x", inline=True)
    embed.add_field(name="NO odds", value=f"{bet.no_odds:.2f}x", inline=True)

    if bet.outcome:
        outcome_text = {
            BetOutcome.YES: f"{YES_EMOJI} YES",
            BetOutcome.NO: f"{NO_EMOJI} NO",
            BetOutcome.REFUND: "↩️ Refund (tie / N/A)",
        }[bet.outcome]
        embed.add_field(name="Outcome", value=outcome_text, inline=True)

    if wagers:
        lines = []
        for w in wagers[:15]:
            lines.append(f"<@{w.user_id}>: {emoji_from_pick(w.pick)} **{w.amount}**")
        if len(wagers) > 15:
            lines.append(f"_…and {len(wagers) - 15} more_")
        embed.add_field(name="Participants", value="\n".join(lines) or "None yet", inline=False)
    elif bet.status == BetStatus.OPEN:
        embed.add_field(
            name="How to join",
            value=(
                f"React {YES_EMOJI} for **YES** or {NO_EMOJI} for **NO**, "
                "then enter your wager amount when prompted."
            ),
            inline=False,
        )

    footer = f"Bet ID: {bet.id}"
    if footer_extra:
        footer = f"{footer_extra} • {footer}"
    embed.set_footer(text=footer)
    return embed


class BetService:
    """Coordinates bet lifecycle operations with balance safety."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def place_or_update_wager(
        self,
        guild_id: int,
        bet_id: int,
        user_id: int,
        pick: WagerPick,
        amount: int,
    ) -> tuple[Wager, int]:
        """
        Place or update a wager, adjusting balances atomically.
        Returns (wager, new_balance).
        """
        if amount <= 0:
            raise ValueError("Wager must be a positive integer.")

        bet = await self.db.get_bet(bet_id)
        if not bet:
            raise ValueError("Bet not found.")
        if bet.guild_id != guild_id:
            raise ValueError("Bet does not belong to this server.")
        if bet.status != BetStatus.OPEN:
            raise ValueError("This bet is no longer open.")
        if datetime.now(timezone.utc) >= bet.close_time:
            raise ValueError("This bet has already closed.")
        if user_id == bet.creator_id:
            raise ValueError("You cannot wager on a bet you created.")

        await self.db.ensure_user(guild_id, user_id)
        existing = await self.db.get_wager(bet_id, user_id)

        # Refund previous wager before deducting the new amount.
        if existing:
            await self.db.adjust_balance(guild_id, user_id, existing.amount)

        try:
            new_balance = await self.db.adjust_balance(guild_id, user_id, -amount)
        except ValueError:
            # Restore previous wager if the new one cannot be funded.
            if existing:
                await self.db.adjust_balance(guild_id, user_id, -existing.amount)
                await self.db.upsert_wager(
                    bet_id, user_id, existing.pick, existing.amount
                )
            raise ValueError("Insufficient balance for this wager.")

        wager = await self.db.upsert_wager(bet_id, user_id, pick, amount)
        return wager, new_balance

    async def remove_wager_and_refund(
        self, guild_id: int, bet_id: int, user_id: int
    ) -> int | None:
        """Remove a user's wager and refund their balance. Returns refund amount."""
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.status != BetStatus.OPEN:
            return None

        wager = await self.db.remove_wager(bet_id, user_id)
        if not wager:
            return None

        await self.db.adjust_balance(guild_id, user_id, wager.amount)
        return wager.amount

    async def close_bet(self, bet_id: int) -> Bet | None:
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.status != BetStatus.OPEN:
            return None
        return await self.db.update_bet_status(bet_id, BetStatus.CLOSED)

    async def cancel_bet(self, bet_id: int) -> list[Wager]:
        """Cancel an unresolved bet and refund all wagers."""
        bet = await self.db.get_bet(bet_id)
        if not bet:
            raise ValueError("Bet not found.")
        if bet.status in (BetStatus.RESOLVED, BetStatus.CANCELLED):
            raise ValueError("Bet cannot be cancelled.")

        wagers = await self.db.get_wagers_for_bet(bet_id)
        for wager in wagers:
            await self.db.adjust_balance(bet.guild_id, wager.user_id, wager.amount)
            await self.db.remove_wager(bet_id, wager.user_id)

        await self.db.update_bet_status(bet_id, BetStatus.CANCELLED)
        return wagers

    async def resolve_bet(
        self, bet_id: int, outcome: BetOutcome
    ) -> tuple[Bet, list[tuple[Wager, int]]]:
        """
        Resolve a closed or open bet. Pays winners (or refunds everyone on REFUND).
        Returns bet and list of (wager, payout) for winners/refunds.
        """
        bet = await self.db.get_bet(bet_id)
        if not bet:
            raise ValueError("Bet not found.")
        if bet.status == BetStatus.RESOLVED:
            raise ValueError("Bet is already resolved.")
        if bet.status == BetStatus.CANCELLED:
            raise ValueError("Bet was cancelled.")

        wagers = await self.db.get_wagers_for_bet(bet_id)
        payouts: list[tuple[Wager, int]] = []

        for wager in wagers:
            payout = compute_payout(wager, bet, outcome)
            if payout > 0:
                await self.db.adjust_balance(bet.guild_id, wager.user_id, payout)
                payouts.append((wager, payout))

        await self.db.update_bet_status(bet_id, BetStatus.RESOLVED, outcome)
        updated = await self.db.get_bet(bet_id)
        assert updated is not None
        return updated, payouts

    async def refund_unresolved_bet(self, bet_id: int) -> tuple[Bet, int] | None:
        """
        Refund all wagers when a closed bet was never resolved.
        Returns (updated_bet, refunded_wager_count) or None if not applicable.
        """
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.status != BetStatus.CLOSED:
            return None

        wagers = await self.db.get_wagers_for_bet(bet_id)
        for wager in wagers:
            await self.db.adjust_balance(bet.guild_id, wager.user_id, wager.amount)
            await self.db.remove_wager(bet_id, wager.user_id)

        updated = await self.db.update_bet_status(bet_id, BetStatus.CANCELLED)
        if not updated:
            return None
        return updated, len(wagers)

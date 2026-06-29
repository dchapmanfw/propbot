"""Business logic for prop bets: duration parsing, payouts, embeds."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import discord

from config import ALLOWED_CHANNEL_ID, NO_EMOJI, STARTING_BALANCE, YES_EMOJI
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


def compute_bookie_reserve(
    wagers: list[Wager], yes_odds: float, no_odds: float
) -> tuple[int, int, int, int]:
    """
    Compute escrow pool size and required bookie reserve.

    Reserve covers the worst-case shortfall if either side wins:
      loss_if_YES = max(0, yes_payouts - pool)
      loss_if_NO  = max(0, no_payouts - pool)
      reserve     = max(loss_if_YES, loss_if_NO)

    Returns (pool, yes_payout_total, no_payout_total, required_reserve).
    """
    pool = sum(w.amount for w in wagers)
    yes_payout = sum(
        int(w.amount * yes_odds) for w in wagers if w.pick == WagerPick.YES
    )
    no_payout = sum(
        int(w.amount * no_odds) for w in wagers if w.pick == WagerPick.NO
    )
    loss_if_yes = max(0, yes_payout - pool)
    loss_if_no = max(0, no_payout - pool)
    required_reserve = max(loss_if_yes, loss_if_no)
    return pool, yes_payout, no_payout, required_reserve


def _hypothetical_wagers(
    bet_id: int,
    wagers: list[Wager],
    user_id: int,
    pick: WagerPick,
    amount: int,
) -> list[Wager]:
    """Build wager list as if user_id placed/updated their position."""
    kept = [w for w in wagers if w.user_id != user_id]
    kept.append(
        Wager(id=0, bet_id=bet_id, user_id=user_id, pick=pick, amount=amount)
    )
    return kept


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
    if bet.status in (BetStatus.OPEN, BetStatus.CLOSED):
        embed.add_field(
            name="Pool / reserve",
            value=f"**{bet.escrow_balance}** in escrow · **{bet.bookie_reserve}** reserved",
            inline=False,
        )

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


def build_help_embed() -> discord.Embed:
    """Build the /help guide embed."""
    channel_note = (
        f"Use <#{ALLOWED_CHANNEL_ID}> for all bot commands and bets."
        if ALLOWED_CHANNEL_ID is not None
        else "Commands work in any channel unless your server configures a dedicated betting channel."
    )

    embed = discord.Embed(
        title="Prop Bet Bot — How to Play",
        description=(
            f"Wager fictional coins on yes/no predictions — **for fun only**. "
            f"Everyone starts with **{STARTING_BALANCE}** coins per server.\n\n"
            f"{channel_note}"
        ),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="For fun only — fictional currency",
        value=(
            "Coins in this bot are **play money** with **no real-world value**. "
            "They cannot be bought, sold, traded, or cashed out.\n"
            "This bot is meant for **entertainment in your server**, not real wagers. "
            "Do not use it for real-money betting, gambling, or anything illegal."
        ),
        inline=False,
    )

    embed.add_field(
        name="1. Create a bet (bookie)",
        value=(
            "`/bet_create question:\"Will X happen?\" duration:2h yes_odds:1.5 no_odds:2.0`\n"
            "You are the **bookie** — wagers go into escrow and you cover payouts from your balance. "
            "You **cannot** wager on your own bet.\n"
            "Duration examples: `30m`, `2h`, `1d`"
        ),
        inline=False,
    )

    embed.add_field(
        name="2. Join a bet",
        value=(
            f"React {YES_EMOJI} for **YES** or {NO_EMOJI} for **NO** on the bet message, "
            "then click **Enter wager** and submit your amount.\n"
            "• Change your mind before close — react again and enter a new amount\n"
            "• Remove your reaction to cancel and refund your wager"
        ),
        inline=False,
    )

    embed.add_field(
        name="3. After betting closes",
        value=(
            "The bet creator (or a server admin) runs `/bet_resolve` with **YES**, **NO**, "
            "or **Refund** (tie / N/A).\n"
            "Winners receive `wager × odds`. Unresolved closed bets are auto-refunded after a grace period."
        ),
        inline=False,
    )

    embed.add_field(
        name="Commands",
        value=(
            "`/balance` — your coin balance\n"
            "`/bet_create` — open a new bet (you are the bookie)\n"
            "`/bet_status bet_id` — bet details and participants\n"
            "`/bet_resolve bet_id outcome` — settle a bet (creator or admin)\n"
            "`/bet_cancel bet_id` — cancel and refund all wagers (creator or admin)\n"
            "`/my_bets` — your recent and active bets\n"
            "`/leaderboard` — top balances in this server\n"
            "`/help` — show this guide"
        ),
        inline=False,
    )

    embed.add_field(
        name="Bookie tips",
        value=(
            "• Set **yes_odds** and **no_odds** separately to shape action on each side\n"
            "• Your balance must cover **reserve** (worst-case payout) as wagers come in\n"
            "• If payouts exceed the pool, the shortfall comes from your balance (can go negative)"
        ),
        inline=False,
    )

    embed.set_footer(
        text="Play-money only — not for real wagers. "
        "Need DMs enabled for wager prompts, or use the in-channel button fallback."
    )
    return embed


class BetService:
    """Coordinates bet lifecycle with bookie escrow and liability reserves."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def _apply_reserve_change(
        self, bet: Bet, new_reserve: int, new_escrow: int
    ) -> None:
        """Lock or release bookie funds when reserve changes."""
        reserve_delta = new_reserve - bet.bookie_reserve
        if reserve_delta > 0:
            await self.db.adjust_balance(
                bet.guild_id, bet.creator_id, -reserve_delta
            )
        elif reserve_delta < 0:
            await self.db.adjust_balance(
                bet.guild_id, bet.creator_id, -reserve_delta
            )
        await self.db.set_bet_escrow(bet.id, new_escrow, new_reserve)

    async def place_or_update_wager(
        self,
        guild_id: int,
        bet_id: int,
        user_id: int,
        pick: WagerPick,
        amount: int,
    ) -> tuple[Wager, int]:
        """
        Place or update a wager. Bettor funds escrow; bookie reserve is adjusted.
        Returns (wager, new_bettor_balance).
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
        await self.db.ensure_user(guild_id, bet.creator_id)
        existing = await self.db.get_wager(bet_id, user_id)
        wagers = await self.db.get_wagers_for_bet(bet_id)

        hypothetical = _hypothetical_wagers(bet_id, wagers, user_id, pick, amount)
        _, _, _, new_reserve = compute_bookie_reserve(
            hypothetical, bet.yes_odds, bet.no_odds
        )
        new_escrow = bet.escrow_balance - (existing.amount if existing else 0) + amount

        reserve_delta = new_reserve - bet.bookie_reserve
        if reserve_delta > 0:
            bookie_balance = await self.db.get_balance(guild_id, bet.creator_id)
            if bookie_balance is None or bookie_balance < reserve_delta:
                raise ValueError(
                    "Bookie does not have enough balance to cover liability for this wager."
                )

        if existing:
            await self.db.adjust_balance(guild_id, user_id, existing.amount)

        try:
            new_balance = await self.db.adjust_balance(guild_id, user_id, -amount)
        except ValueError:
            if existing:
                await self.db.adjust_balance(guild_id, user_id, -existing.amount)
                await self.db.upsert_wager(
                    bet_id, user_id, existing.pick, existing.amount
                )
            raise ValueError("Insufficient balance for this wager.")

        bookie_adjusted = False
        escrow_updated = False
        try:
            if reserve_delta != 0:
                await self.db.adjust_balance(
                    guild_id, bet.creator_id, -reserve_delta
                )
                bookie_adjusted = True
            await self.db.set_bet_escrow(bet.id, new_escrow, new_reserve)
            escrow_updated = True
            wager = await self.db.upsert_wager(bet_id, user_id, pick, amount)
        except Exception:
            await self.db.adjust_balance(guild_id, user_id, amount)
            if existing:
                await self.db.adjust_balance(guild_id, user_id, -existing.amount)
                await self.db.upsert_wager(
                    bet_id, user_id, existing.pick, existing.amount
                )
            if bookie_adjusted:
                await self.db.adjust_balance(
                    guild_id, bet.creator_id, reserve_delta
                )
            if escrow_updated:
                await self.db.set_bet_escrow(
                    bet.id, bet.escrow_balance, bet.bookie_reserve
                )
            raise

        return wager, new_balance

    async def remove_wager_and_refund(
        self, guild_id: int, bet_id: int, user_id: int
    ) -> int | None:
        """Remove a user's wager, refund bettor, and release bookie reserve."""
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.status != BetStatus.OPEN:
            return None

        wager = await self.db.remove_wager(bet_id, user_id)
        if not wager:
            return None

        await self.db.adjust_balance(guild_id, user_id, wager.amount)
        wagers = await self.db.get_wagers_for_bet(bet_id)
        _, _, _, new_reserve = compute_bookie_reserve(
            wagers, bet.yes_odds, bet.no_odds
        )
        new_escrow = bet.escrow_balance - wager.amount
        await self._apply_reserve_change(
            bet, new_reserve=new_reserve, new_escrow=new_escrow
        )
        return wager.amount

    async def close_bet(self, bet_id: int) -> Bet | None:
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.status != BetStatus.OPEN:
            return None
        return await self.db.update_bet_status(bet_id, BetStatus.CLOSED)

    async def cancel_bet(self, bet_id: int) -> list[Wager]:
        """Cancel an unresolved bet, refund bettors from escrow, release bookie reserve."""
        bet = await self.db.get_bet(bet_id)
        if not bet:
            raise ValueError("Bet not found.")
        if bet.status in (BetStatus.RESOLVED, BetStatus.CANCELLED):
            raise ValueError("Bet cannot be cancelled.")

        wagers = await self.db.get_wagers_for_bet(bet_id)
        for wager in wagers:
            await self.db.adjust_balance(bet.guild_id, wager.user_id, wager.amount)
            await self.db.remove_wager(bet_id, wager.user_id)

        if bet.bookie_reserve > 0:
            await self.db.adjust_balance(
                bet.guild_id, bet.creator_id, bet.bookie_reserve
            )
        await self.db.set_bet_escrow(bet.id, 0, 0)
        await self.db.update_bet_status(bet_id, BetStatus.CANCELLED)
        return wagers

    async def resolve_bet(
        self, bet_id: int, outcome: BetOutcome
    ) -> tuple[Bet, list[tuple[Wager, int]]]:
        """
        Resolve a bet. Winners are paid from escrow; shortfall comes from the bookie
        (balance may go negative). Surplus escrow goes to the bookie. Reserve is released.
        """
        bet = await self.db.get_bet(bet_id)
        if not bet:
            raise ValueError("Bet not found.")
        if bet.status == BetStatus.RESOLVED:
            raise ValueError("Bet is already resolved.")
        if bet.status == BetStatus.CANCELLED:
            raise ValueError("Bet was cancelled.")
        if bet.status != BetStatus.CLOSED:
            raise ValueError("Bet must be closed before it can be resolved.")

        wagers = await self.db.get_wagers_for_bet(bet_id)
        escrow_balance = bet.escrow_balance
        bookie_reserve = bet.bookie_reserve

        claimed = await self.db.claim_bet_for_resolution(bet_id, outcome)
        if not claimed:
            raise ValueError("Bet is already resolved.")

        payouts: list[tuple[Wager, int]] = []
        total_payout = 0

        for wager in wagers:
            payout = compute_payout(wager, bet, outcome)
            if payout > 0:
                await self.db.adjust_balance(bet.guild_id, wager.user_id, payout)
                payouts.append((wager, payout))
                total_payout += payout

        # Escrow funds the pool; bookie nets (escrow - payouts) plus locked reserve returned.
        bookie_settlement = escrow_balance - total_payout + bookie_reserve
        await self.db.adjust_balance_allow_negative(
            bet.guild_id, bet.creator_id, bookie_settlement
        )
        await self.db.set_bet_escrow(bet.id, 0, 0)

        return claimed, payouts

    async def refund_unresolved_bet(self, bet_id: int) -> tuple[Bet, int] | None:
        """
        Refund all wagers from escrow when a closed bet was never resolved.
        Returns (updated_bet, refunded_wager_count) or None if not applicable.
        """
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.status != BetStatus.CLOSED:
            return None

        wagers = await self.db.get_wagers_for_bet(bet_id)
        for wager in wagers:
            await self.db.adjust_balance(bet.guild_id, wager.user_id, wager.amount)
            await self.db.remove_wager(bet_id, wager.user_id)

        if bet.bookie_reserve > 0:
            await self.db.adjust_balance(
                bet.guild_id, bet.creator_id, bet.bookie_reserve
            )
        await self.db.set_bet_escrow(bet.id, 0, 0)

        updated = await self.db.update_bet_status(bet_id, BetStatus.CANCELLED)
        if not updated:
            return None
        return updated, len(wagers)

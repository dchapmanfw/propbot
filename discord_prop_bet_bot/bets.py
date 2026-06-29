"""Business logic for prop bets: duration parsing, payouts, embeds."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import discord

from config import ALLOWED_CHANNEL_ID, NO_EMOJI, STARTING_BALANCE, YES_EMOJI
from database import Database
from models import Bet, BetOutcome, BetStatus, UserBalance, Wager, WagerPick

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


def compute_max_wager(balance: int, existing_wager_amount: int = 0) -> int:
    """Maximum wager a bettor can place (existing escrow on this bet is refunded first)."""
    return max(0, balance + existing_wager_amount)


_BOOKIE_MAX_SEARCH = 10_000_000


def compute_bookie_max_additional_wager(
    bet: Bet,
    wagers: list[Wager],
    pick: WagerPick,
    bookie_balance: int,
) -> int | None:
    """
    Largest wager a new bettor can add on pick without exceeding bookie reserve.

    Returns None when the bookie can accept any practical amount on that side.
    """

    def accepts(amount: int) -> bool:
        if amount <= 0:
            return amount == 0
        hypothetical = list(wagers) + [
            Wager(id=0, bet_id=bet.id, user_id=-1, pick=pick, amount=amount)
        ]
        _, _, _, new_reserve = compute_bookie_reserve(
            hypothetical, bet.yes_odds, bet.no_odds
        )
        reserve_delta = new_reserve - bet.bookie_reserve
        return reserve_delta <= 0 or bookie_balance >= reserve_delta

    if not accepts(1):
        return 0

    lo, hi = 1, 1
    while hi < _BOOKIE_MAX_SEARCH and accepts(hi):
        lo = hi
        hi *= 2

    if hi >= _BOOKIE_MAX_SEARCH and accepts(_BOOKIE_MAX_SEARCH):
        return None

    best = lo
    left, right = lo, min(hi - 1, _BOOKIE_MAX_SEARCH)
    while left <= right:
        mid = (left + right) // 2
        if accepts(mid):
            best = mid
            left = mid + 1
        else:
            right = mid - 1
    return best


def format_side_max_bet(max_wager: int | None) -> str:
    """Format bookie-side max bet for embed display."""
    if max_wager is None:
        return "No limit"
    return str(max_wager)


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


def format_anti_prestige(reset_count: int) -> str:
    """Leaderboard suffix for users who have reset."""
    if reset_count <= 0:
        return ""
    return f" · ↩️×{reset_count}"


def format_balance_message(user: UserBalance) -> str:
    """User-facing balance text including debt and anti-prestige."""
    from economy import REDEMPTION_COST

    if user.balance < 0:
        balance_line = f"Your balance: **{user.balance}** coins (bookie debt)."
    else:
        balance_line = f"Your balance: **{user.balance}** coins."

    lines = [balance_line]
    if user.reset_count > 0:
        lines.append(
            f"Anti-prestige: **↩️×{user.reset_count}** "
            f"(pay **{REDEMPTION_COST}** with `/redeem` to clear one)"
        )
    return "\n".join(lines)


def build_leaderboard_description(rows: list[UserBalance]) -> str:
    """Format leaderboard rows with anti-prestige markers and optional tier headers."""
    medals = ["🥇", "🥈", "🥉"]
    has_clean = any(row.reset_count == 0 for row in rows)
    has_reset = any(row.reset_count > 0 for row in rows)
    use_tier_headers = has_clean and has_reset

    lines: list[str] = []
    current_tier: str | None = None

    for idx, row in enumerate(rows):
        tier = "clean" if row.reset_count == 0 else "reset"
        if use_tier_headers and tier != current_tier:
            if lines:
                lines.append("")
            lines.append("**No resets**" if tier == "clean" else "**↩️×1+**")
            current_tier = tier

        prefix = medals[idx] if idx < 3 else f"{idx + 1}."
        prestige = format_anti_prestige(row.reset_count)
        lines.append(
            f"{prefix} <@{row.user_id}> — **{row.balance}** coins{prestige}"
        )

    return "\n".join(lines)


def build_bet_embed(
    bet: Bet,
    creator: discord.abc.User | None = None,
    wagers: list[Wager] | None = None,
    footer_extra: str | None = None,
    bookie_balance: int | None = None,
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
    wagers_list = wagers or []
    if bet.status == BetStatus.OPEN and bookie_balance is not None:
        max_yes = compute_bookie_max_additional_wager(
            bet, wagers_list, WagerPick.YES, bookie_balance
        )
        max_no = compute_bookie_max_additional_wager(
            bet, wagers_list, WagerPick.NO, bookie_balance
        )
        embed.add_field(
            name="YES odds",
            value=(
                f"{bet.yes_odds:.2f}x\n"
                f"Max bet: **{format_side_max_bet(max_yes)}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="NO odds",
            value=(
                f"{bet.no_odds:.2f}x\n"
                f"Max bet: **{format_side_max_bet(max_no)}**"
            ),
            inline=True,
        )
    else:
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
            "`/reset` — bail out to starting cash (adds anti-prestige ↩️)\n"
            "`/redeem` — pay 2× starting balance to clear one ↩️\n"
            "`/help` — show this guide"
        ),
        inline=False,
    )

    embed.add_field(
        name="Bookie tips",
        value=(
            "• Set **yes_odds** and **no_odds** separately to shape action on each side\n"
            "• Your balance must cover **reserve** (worst-case payout) as wagers come in\n"
            "• If payouts exceed the pool, the shortfall comes from your balance (can go negative)\n"
            "• Use `/reset` when below starting balance to bail out — costs leaderboard prestige (↩️)"
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
        self,
        bet: Bet,
        new_reserve: int,
        new_escrow: int,
        *,
        commit: bool = True,
    ) -> None:
        """Lock or release bookie funds when reserve changes."""
        reserve_delta = new_reserve - bet.bookie_reserve
        if reserve_delta != 0:
            await self.db.adjust_balance(
                bet.guild_id, bet.creator_id, -reserve_delta, commit=commit
            )
        await self.db.set_bet_escrow(
            bet.id, new_escrow, new_reserve, commit=commit
        )

    async def _refund_wagers_and_release_reserve(
        self,
        bet: Bet,
        wagers: list[Wager],
    ) -> None:
        """Refund all wagers, clear escrow, and release bookie reserve."""
        for wager in wagers:
            await self.db.adjust_balance(
                bet.guild_id, wager.user_id, wager.amount, commit=False
            )
        await self.db.remove_all_wagers_for_bet(bet.id, commit=False)
        if bet.bookie_reserve > 0:
            await self.db.adjust_balance(
                bet.guild_id, bet.creator_id, bet.bookie_reserve, commit=False
            )
        await self.db.set_bet_escrow(bet.id, 0, 0, commit=False)

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

        async with self.db.transaction():
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

            await self.db.ensure_users(
                guild_id, [user_id, bet.creator_id], commit=False
            )
            existing = await self.db.get_wager(bet_id, user_id)
            wagers = await self.db.get_wagers_for_bet(bet_id)

            hypothetical = _hypothetical_wagers(bet_id, wagers, user_id, pick, amount)
            _, _, _, new_reserve = compute_bookie_reserve(
                hypothetical, bet.yes_odds, bet.no_odds
            )
            new_escrow = (
                bet.escrow_balance - (existing.amount if existing else 0) + amount
            )

            reserve_delta = new_reserve - bet.bookie_reserve
            if reserve_delta > 0:
                bookie_balance = await self.db.get_balance(guild_id, bet.creator_id)
                if bookie_balance is None or bookie_balance < reserve_delta:
                    raise ValueError(
                        "Bookie does not have enough balance to cover liability for this wager."
                    )

            if existing:
                await self.db.adjust_balance(
                    guild_id, user_id, existing.amount, commit=False
                )

            try:
                new_balance = await self.db.adjust_balance(
                    guild_id, user_id, -amount, commit=False
                )
            except ValueError:
                raise ValueError("Insufficient balance for this wager.") from None

            if reserve_delta != 0:
                await self.db.adjust_balance(
                    guild_id, bet.creator_id, -reserve_delta, commit=False
                )
            await self.db.set_bet_escrow(
                bet.id, new_escrow, new_reserve, commit=False
            )
            wager = await self.db.upsert_wager(
                bet_id, user_id, pick, amount, commit=False
            )

        return wager, new_balance

    async def remove_wager_and_refund(
        self, guild_id: int, bet_id: int, user_id: int
    ) -> int | None:
        """Remove a user's wager, refund bettor, and release bookie reserve."""
        async with self.db.transaction():
            bet = await self.db.get_bet(bet_id)
            if not bet or bet.status != BetStatus.OPEN:
                return None

            wager = await self.db.get_wager(bet_id, user_id)
            if not wager:
                return None

            await self.db.adjust_balance(
                guild_id, user_id, wager.amount, commit=False
            )
            await self.db.remove_wager(bet_id, user_id, commit=False)

            wagers = await self.db.get_wagers_for_bet(bet_id)
            _, _, _, new_reserve = compute_bookie_reserve(
                wagers, bet.yes_odds, bet.no_odds
            )
            new_escrow = bet.escrow_balance - wager.amount
            await self._apply_reserve_change(
                bet,
                new_reserve=new_reserve,
                new_escrow=new_escrow,
                commit=False,
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
        bet_snapshot = bet

        async with self.db.transaction():
            claimed = await self.db.claim_bet_for_cancellation(
                bet_id,
                from_statuses=(BetStatus.OPEN, BetStatus.CLOSED),
                commit=False,
            )
            if not claimed:
                raise ValueError("Bet cannot be cancelled.")

            await self._refund_wagers_and_release_reserve(bet_snapshot, wagers)

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

        async with self.db.transaction():
            claimed = await self.db.claim_bet_for_resolution(
                bet_id, outcome, commit=False
            )
            if not claimed:
                raise ValueError("Bet is already resolved.")

            payouts: list[tuple[Wager, int]] = []
            total_payout = 0

            for wager in wagers:
                payout = compute_payout(wager, bet, outcome)
                if payout > 0:
                    await self.db.adjust_balance(
                        bet.guild_id, wager.user_id, payout, commit=False
                    )
                    payouts.append((wager, payout))
                    total_payout += payout

            bookie_settlement = escrow_balance - total_payout + bookie_reserve
            await self.db.adjust_balance_allow_negative(
                bet.guild_id, bet.creator_id, bookie_settlement, commit=False
            )
            await self.db.set_bet_escrow(bet.id, 0, 0, commit=False)

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
        bet_snapshot = bet

        async with self.db.transaction():
            claimed = await self.db.claim_bet_for_cancellation(
                bet_id,
                from_statuses=(BetStatus.CLOSED,),
                commit=False,
            )
            if not claimed:
                return None

            await self._refund_wagers_and_release_reserve(bet_snapshot, wagers)

        return claimed, len(wagers)

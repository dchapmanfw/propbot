"""Polymarket-style prediction markets with LMSR pricing."""

from __future__ import annotations

import math

import discord

from config import DEFAULT_MARKET_LIQUIDITY, MAX_MARKET_TRADE_COINS, NO_EMOJI, YES_EMOJI
from database import Database
from lmsr import (
    format_price_cents,
    lmsr_buy_cost,
    lmsr_initial_subsidy,
    lmsr_price_no,
    lmsr_price_yes,
    lmsr_sell_proceeds,
    shares_for_budget,
)
from models import Bet, BetKind, BetOutcome, BetStatus, MarketPosition, WagerPick


def _payout_shares(shares: float) -> int:
    """Convert fractional shares to integer coin payout on resolution."""
    return int(round(shares))


def format_shares(shares: float) -> str:
    if shares >= 100:
        return f"{shares:.1f}"
    if shares >= 10:
        return f"{shares:.2f}"
    return f"{shares:.3f}"


def build_market_embed(
    bet: Bet,
    creator: discord.abc.User | None = None,
    positions: list[MarketPosition] | None = None,
    footer_extra: str | None = None,
) -> discord.Embed:
    """Build the public embed for a prediction market."""
    color = {
        BetStatus.OPEN: discord.Color.teal(),
        BetStatus.CLOSED: discord.Color.orange(),
        BetStatus.RESOLVED: discord.Color.blue(),
        BetStatus.CANCELLED: discord.Color.dark_grey(),
    }[bet.status]

    yes_price = lmsr_price_yes(bet.q_yes, bet.q_no, bet.liquidity_b)
    no_price = lmsr_price_no(bet.q_yes, bet.q_no, bet.liquidity_b)

    embed = discord.Embed(
        title=f"Prediction Market #{bet.id}",
        description=bet.question,
        color=color,
        timestamp=bet.created_at,
    )

    creator_name = creator.mention if creator else f"<@{bet.creator_id}>"
    embed.add_field(name="Creator", value=creator_name, inline=True)
    embed.add_field(name="Status", value=_status_label(bet.status), inline=True)
    embed.add_field(
        name="Closes",
        value=discord.utils.format_dt(bet.close_time, style="R"),
        inline=True,
    )

    if bet.status == BetStatus.OPEN:
        embed.add_field(
            name=f"{YES_EMOJI} YES",
            value=f"**{format_price_cents(yes_price)}**",
            inline=True,
        )
        embed.add_field(
            name=f"{NO_EMOJI} NO",
            value=f"**{format_price_cents(no_price)}**",
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)
    else:
        embed.add_field(
            name="Final prices",
            value=(
                f"{YES_EMOJI} {format_price_cents(yes_price)} · "
                f"{NO_EMOJI} {format_price_cents(no_price)}"
            ),
            inline=False,
        )

    if bet.status in (BetStatus.OPEN, BetStatus.CLOSED):
        embed.add_field(
            name="Pool",
            value=f"**{bet.escrow_balance}** coins in market escrow",
            inline=False,
        )

    if bet.outcome:
        outcome_text = {
            BetOutcome.YES: f"{YES_EMOJI} YES",
            BetOutcome.NO: f"{NO_EMOJI} NO",
            BetOutcome.REFUND: "↩️ Refund (tie / N/A)",
        }[bet.outcome]
        embed.add_field(name="Outcome", value=outcome_text, inline=True)

    if positions:
        lines = _position_lines(positions)
        embed.add_field(
            name="Positions",
            value="\n".join(lines) or "None yet",
            inline=False,
        )
    elif bet.status == BetStatus.OPEN:
        embed.add_field(
            name="How to trade",
            value=(
                f"React {YES_EMOJI} or {NO_EMOJI} to **buy** shares at the current price.\n"
                f"Max **{MAX_MARKET_TRADE_COINS}** coins per buy · "
                "use `/market_sell` to sell before close.\n"
                "Each winning share pays **1 coin** at resolution."
            ),
            inline=False,
        )

    footer = f"Market ID: {bet.id} · LMSR liquidity: {bet.liquidity_b:g}"
    if footer_extra:
        footer = f"{footer_extra} • {footer}"
    embed.set_footer(text=footer)
    return embed


def _status_label(status: BetStatus) -> str:
    return {
        BetStatus.OPEN: "🟢 Open",
        BetStatus.CLOSED: "🔴 Closed (awaiting resolution)",
        BetStatus.RESOLVED: "✅ Resolved",
        BetStatus.CANCELLED: "🚫 Cancelled",
    }[status]


def _position_lines(positions: list[MarketPosition], limit: int = 15) -> list[str]:
    aggregated: dict[int, dict[WagerPick, float]] = {}
    for pos in positions:
        aggregated.setdefault(pos.user_id, {})
        aggregated[pos.user_id][pos.side] = aggregated[pos.user_id].get(pos.side, 0) + pos.shares

    lines: list[str] = []
    sorted_users = sorted(
        aggregated.items(),
        key=lambda item: sum(item[1].values()),
        reverse=True,
    )
    for user_id, sides in sorted_users[:limit]:
        parts = []
        for side in (WagerPick.YES, WagerPick.NO):
            if side in sides:
                emoji = YES_EMOJI if side == WagerPick.YES else NO_EMOJI
                parts.append(f"{emoji} {format_shares(sides[side])}")
        lines.append(f"<@{user_id}>: {' · '.join(parts)}")
    if len(sorted_users) > limit:
        lines.append(f"_…and {len(sorted_users) - limit} more_")
    return lines


class MarketService:
    """Coordinates prediction-market lifecycle with LMSR pricing."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_market(
        self,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        question: str,
        close_time,
    ) -> Bet:
        b = DEFAULT_MARKET_LIQUIDITY
        if b <= 0:
            raise ValueError("Market liquidity is misconfigured.")
        subsidy = lmsr_initial_subsidy(b)
        return await self.db.create_market(
            guild_id,
            channel_id,
            creator_id,
            question,
            close_time,
            b,
            subsidy,
        )

    async def buy_shares(
        self,
        guild_id: int,
        bet_id: int,
        user_id: int,
        side: WagerPick,
        coin_amount: int,
    ) -> tuple[MarketPosition, int, float]:
        """
        Spend up to `coin_amount` coins to buy shares on `side`.
        Returns (position, new_balance, shares_bought).
        """
        if coin_amount <= 0:
            raise ValueError("Amount must be a positive integer.")
        if coin_amount > MAX_MARKET_TRADE_COINS:
            raise ValueError(
                f"Maximum trade size is {MAX_MARKET_TRADE_COINS} coins per purchase."
            )

        async with self.db.transaction():
            bet = await self._require_open_market(guild_id, bet_id)
            await self.db.ensure_user(guild_id, user_id, commit=False)

            shares, cost = shares_for_budget(
                bet.q_yes, bet.q_no, side, coin_amount, bet.liquidity_b
            )
            if shares <= 1e-9 or cost <= 0:
                raise ValueError("Amount too small to buy any shares at current prices.")

            new_balance = await self.db.adjust_balance(
                guild_id, user_id, -cost, commit=False
            )

            new_q_yes = bet.q_yes + (shares if side == WagerPick.YES else 0)
            new_q_no = bet.q_no + (shares if side == WagerPick.NO else 0)
            new_escrow = bet.escrow_balance + cost

            existing = await self.db.get_market_position(bet_id, user_id, side)
            new_shares = (existing.shares if existing else 0.0) + shares

            await self.db.set_market_state(
                bet_id, new_q_yes, new_q_no, new_escrow, commit=False
            )
            position = await self.db.upsert_market_position(
                bet_id, user_id, side, new_shares, commit=False
            )

        return position, new_balance, shares

    async def sell_shares(
        self,
        guild_id: int,
        bet_id: int,
        user_id: int,
        side: WagerPick,
        shares: float,
    ) -> tuple[int, float]:
        """
        Sell `shares` back to the market before close.
        Returns (new_balance, proceeds).
        """
        if shares <= 0:
            raise ValueError("Shares to sell must be positive.")

        async with self.db.transaction():
            bet = await self._require_open_market(guild_id, bet_id)
            position = await self.db.get_market_position(bet_id, user_id, side)
            if not position or position.shares + 1e-9 < shares:
                raise ValueError("You do not hold enough shares to sell.")

            raw_proceeds = lmsr_sell_proceeds(
                bet.q_yes, bet.q_no, side, shares, bet.liquidity_b
            )
            proceeds = int(math.floor(raw_proceeds))
            if proceeds <= 0:
                raise ValueError("Sale value is too small.")

            if bet.escrow_balance < proceeds:
                raise ValueError("Market escrow cannot cover this sale.")

            new_q_yes = bet.q_yes - (shares if side == WagerPick.YES else 0)
            new_q_no = bet.q_no - (shares if side == WagerPick.NO else 0)
            new_escrow = bet.escrow_balance - proceeds
            remaining = position.shares - shares

            new_balance = await self.db.adjust_balance(
                guild_id, user_id, proceeds, commit=False
            )
            await self.db.set_market_state(
                bet_id, new_q_yes, new_q_no, new_escrow, commit=False
            )
            if remaining <= 1e-9:
                await self.db.upsert_market_position(
                    bet_id, user_id, side, 0.0, commit=False
                )
            else:
                await self.db.upsert_market_position(
                    bet_id, user_id, side, remaining, commit=False
                )

        return new_balance, shares

    async def close_market(self, bet_id: int) -> Bet | None:
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.bet_kind != BetKind.MARKET or bet.status != BetStatus.OPEN:
            return None
        return await self.db.update_bet_status(bet_id, BetStatus.CLOSED)

    async def cancel_market(self, bet_id: int) -> int:
        """Cancel market and liquidate all positions at current LMSR prices."""
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.bet_kind != BetKind.MARKET:
            raise ValueError("Market not found.")
        if bet.status in (BetStatus.RESOLVED, BetStatus.CANCELLED):
            raise ValueError("Market cannot be cancelled.")

        positions = await self.db.get_market_positions_for_bet(bet_id)
        bet_snapshot = bet

        async with self.db.transaction():
            claimed = await self.db.claim_bet_for_cancellation(
                bet_id,
                from_statuses=(BetStatus.OPEN, BetStatus.CLOSED),
                commit=False,
            )
            if not claimed:
                raise ValueError("Market cannot be cancelled.")

            await self._liquidate_positions(bet_snapshot, positions)

        return len(positions)

    async def resolve_market(
        self, bet_id: int, outcome: BetOutcome
    ) -> tuple[Bet, list[tuple[int, int, WagerPick]]]:
        """Resolve market — winning shares pay 1 coin each."""
        if outcome == BetOutcome.REFUND:
            return await self._resolve_refund(bet_id)

        bet = await self.db.get_bet(bet_id)
        if not bet or bet.bet_kind != BetKind.MARKET:
            raise ValueError("Market not found.")
        if bet.status == BetStatus.RESOLVED:
            raise ValueError("Market is already resolved.")
        if bet.status == BetStatus.CANCELLED:
            raise ValueError("Market was cancelled.")
        if bet.status != BetStatus.CLOSED:
            raise ValueError("Market must be closed before it can be resolved.")

        positions = await self.db.get_market_positions_for_bet(bet_id)
        winning_side = WagerPick.YES if outcome == BetOutcome.YES else WagerPick.NO

        async with self.db.transaction():
            claimed = await self.db.claim_bet_for_resolution(
                bet_id, outcome, commit=False
            )
            if not claimed:
                raise ValueError("Market is already resolved.")

            payouts: list[tuple[int, int, WagerPick]] = []
            total_payout = 0
            for pos in positions:
                if pos.side != winning_side:
                    continue
                payout = _payout_shares(pos.shares)
                if payout <= 0:
                    continue
                await self.db.adjust_balance(
                    bet.guild_id, pos.user_id, payout, commit=False
                )
                payouts.append((pos.user_id, payout, pos.side))
                total_payout += payout

            if total_payout > bet.escrow_balance:
                raise ValueError(
                    "Market escrow cannot cover payouts. Contact an admin."
                )

            await self.db.set_market_state(bet_id, bet.q_yes, bet.q_no, 0, commit=False)
            await self.db.remove_all_market_positions_for_bet(bet_id, commit=False)

        return claimed, payouts

    async def refund_unresolved_market(
        self, bet_id: int
    ) -> tuple[Bet, int] | None:
        """Liquidate positions when a closed market was never resolved."""
        bet = await self.db.get_bet(bet_id)
        if (
            not bet
            or bet.bet_kind != BetKind.MARKET
            or bet.status != BetStatus.CLOSED
        ):
            return None

        positions = await self.db.get_market_positions_for_bet(bet_id)
        bet_snapshot = bet

        async with self.db.transaction():
            claimed = await self.db.claim_bet_for_cancellation(
                bet_id,
                from_statuses=(BetStatus.CLOSED,),
                commit=False,
            )
            if not claimed:
                return None

            await self._liquidate_positions(bet_snapshot, positions)

        return claimed, len(positions)

    async def _resolve_refund(self, bet_id: int) -> tuple[Bet, list[tuple[int, int, WagerPick]]]:
        bet = await self.db.get_bet(bet_id)
        if not bet or bet.bet_kind != BetKind.MARKET:
            raise ValueError("Market not found.")
        if bet.status != BetStatus.CLOSED:
            raise ValueError("Market must be closed before it can be resolved.")

        positions = await self.db.get_market_positions_for_bet(bet_id)
        bet_snapshot = bet

        async with self.db.transaction():
            claimed = await self.db.claim_bet_for_resolution(
                bet_id, BetOutcome.REFUND, commit=False
            )
            if not claimed:
                raise ValueError("Market is already resolved.")

            await self._liquidate_positions(bet_snapshot, positions)

        return claimed, []

    async def _liquidate_positions(
        self, bet: Bet, positions: list[MarketPosition]
    ) -> None:
        """Sell all held shares back to the LMSR pool and credit users."""
        q_yes, q_no, escrow = bet.q_yes, bet.q_no, bet.escrow_balance

        for pos in positions:
            if pos.shares <= 1e-9:
                continue
            proceeds = int(
                math.floor(
                    lmsr_sell_proceeds(q_yes, q_no, pos.side, pos.shares, bet.liquidity_b)
                )
            )
            if proceeds > 0:
                proceeds = min(proceeds, escrow)
                await self.db.adjust_balance(
                    bet.guild_id, pos.user_id, proceeds, commit=False
                )
                escrow -= proceeds
            if pos.side == WagerPick.YES:
                q_yes -= pos.shares
            else:
                q_no -= pos.shares

        await self.db.set_market_state(bet.id, q_yes, q_no, escrow, commit=False)
        await self.db.remove_all_market_positions_for_bet(bet.id, commit=False)

    async def _require_open_market(self, guild_id: int, bet_id: int) -> Bet:
        from datetime import datetime, timezone

        bet = await self.db.get_bet(bet_id)
        if not bet or bet.bet_kind != BetKind.MARKET:
            raise ValueError("Market not found.")
        if bet.guild_id != guild_id:
            raise ValueError("Market does not belong to this server.")
        if bet.status != BetStatus.OPEN:
            raise ValueError("This market is no longer open.")
        if datetime.now(timezone.utc) >= bet.close_time:
            raise ValueError("This market has already closed.")
        return bet

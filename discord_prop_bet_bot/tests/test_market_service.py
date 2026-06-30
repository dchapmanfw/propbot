"""Tests for prediction market service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lmsr import lmsr_price_yes
from markets import MarketService, build_markets_list_embed
from models import BetKind, BetOutcome, BetStatus, WagerPick


@pytest.fixture
async def market(db):
    service = MarketService(db)
    close = datetime.now(timezone.utc) + timedelta(hours=2)
    return await service.create_market(
        guild_id=1,
        channel_id=10,
        creator_id=100,
        question="Will it rain?",
        close_time=close,
    )


async def test_create_market_seeds_escrow(db, market):
    assert market.bet_kind == BetKind.MARKET
    assert market.escrow_balance > 0
    assert market.q_yes == 0
    assert market.q_no == 0
    assert lmsr_price_yes(market.q_yes, market.q_no, market.liquidity_b) == pytest.approx(
        0.5
    )


async def test_buy_shares_updates_price_and_position(db, market):
    service = MarketService(db)
    await db.ensure_user(1, 200)

    position, balance, shares = await service.buy_shares(
        1, market.id, 200, WagerPick.YES, 50
    )
    updated = await db.get_bet(market.id)
    assert updated is not None
    assert shares > 0
    assert position.shares == pytest.approx(shares)
    assert balance == 1000 - 50
    assert updated.q_yes == pytest.approx(shares)
    assert lmsr_price_yes(updated.q_yes, updated.q_no, updated.liquidity_b) > 0.5


async def test_sell_shares_after_buy(db, market):
    service = MarketService(db)
    await db.ensure_user(1, 200)
    await service.buy_shares(1, market.id, 200, WagerPick.YES, 50)
    position = await db.get_market_position(market.id, 200, WagerPick.YES)
    assert position is not None

    balance, sold = await service.sell_shares(
        1, market.id, 200, WagerPick.YES, position.shares / 2
    )
    assert sold == position.shares / 2
    assert balance > 950


async def test_resolve_market_pays_winning_shares(db, market):
    service = MarketService(db)
    await db.ensure_user(1, 200)
    await db.ensure_user(1, 300)

    await service.buy_shares(1, market.id, 200, WagerPick.YES, 50)
    await service.buy_shares(1, market.id, 300, WagerPick.NO, 50)

    await service.close_market(market.id)
    resolved, payouts = await service.resolve_market(market.id, BetOutcome.YES)

    assert resolved.status == BetStatus.RESOLVED
    assert resolved.outcome == BetOutcome.YES
    yes_payouts = [p for p in payouts if p[2] == WagerPick.YES]
    assert len(yes_payouts) == 1
    assert yes_payouts[0][0] == 200
    assert yes_payouts[0][1] > 0


async def test_cancel_liquidates_positions(db, market):
    service = MarketService(db)
    await db.ensure_user(1, 200)
    start = await db.get_balance(1, 200)
    await service.buy_shares(1, market.id, 200, WagerPick.YES, 50)
    mid = await db.get_balance(1, 200)

    count = await service.cancel_market(market.id)
    end = await db.get_balance(1, 200)
    bet = await db.get_bet(market.id)

    assert count == 1
    assert bet is not None
    assert bet.status == BetStatus.CANCELLED
    assert end > mid
    assert end <= start


async def test_buy_shares_rejects_over_trade_cap(db, market):
    service = MarketService(db)
    await db.ensure_user(1, 200)

    with pytest.raises(ValueError, match="Maximum trade size"):
        await service.buy_shares(1, market.id, 200, WagerPick.YES, 51)


async def test_build_markets_list_embed_empty_and_populated(db, market):
    empty = build_markets_list_embed([], guild_id=1)
    assert "No outstanding" in empty.description

    populated = build_markets_list_embed([market], guild_id=1)
    assert "Outstanding markets (1)" in populated.title
    assert "Will it rain?" in populated.description

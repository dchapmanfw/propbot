"""Bookie escrow, reserve, and settlement integration tests."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from bets import BetService
from config import STARTING_BALANCE
from database import Database
from models import BetOutcome, BetStatus, WagerPick

BOOKIE_ID = 99


@pytest.fixture
async def service():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.connect()
    svc = BetService(db)
    yield svc, db
    await db.close()
    os.unlink(path)


async def _open_bet(
    db: Database,
    *,
    yes_odds: float = 2.0,
    no_odds: float = 1.5,
) -> int:
    close = datetime.now(timezone.utc) + timedelta(hours=2)
    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=BOOKIE_ID,
        question="Test bet?",
        close_time=close,
        yes_odds=yes_odds,
        no_odds=no_odds,
    )
    await db.ensure_user(1, BOOKIE_ID)
    return bet.id


async def _total_supply(db: Database, guild_id: int = 1) -> int:
    """Sum of all user balances plus escrow and locked reserve in open/closed bets."""
    cursor = await db.conn.execute(
        "SELECT COALESCE(SUM(balance), 0) FROM users WHERE guild_id = ?", (guild_id,)
    )
    row = await cursor.fetchone()
    user_total = int(row[0])
    cursor = await db.conn.execute(
        """
        SELECT COALESCE(SUM(escrow_balance), 0), COALESCE(SUM(bookie_reserve), 0)
        FROM bets WHERE guild_id = ?
        """,
        (guild_id,),
    )
    row = await cursor.fetchone()
    return user_total + int(row[0]) + int(row[1])


@pytest.mark.asyncio
async def test_wager_moves_funds_to_escrow(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 50)

    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 200)

    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.escrow_balance == 200
    assert await db.get_balance(1, 50) == STARTING_BALANCE - 200
    assert await _total_supply(db) == STARTING_BALANCE * 2  # bookie + bettor


@pytest.mark.asyncio
async def test_one_sided_wager_locks_bookie_reserve(service):
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=1.5, no_odds=2.0)

    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)

    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.bookie_reserve == 50
    assert await db.get_balance(1, BOOKIE_ID) == STARTING_BALANCE - 50


@pytest.mark.asyncio
async def test_bookie_insufficient_reserve_rejects_wager(service):
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=1.5, no_odds=2.0)

    await db.ensure_user(1, 50)
    await db.adjust_balance(1, BOOKIE_ID, -(STARTING_BALANCE - 10))

    with pytest.raises(ValueError, match="Bookie does not have enough balance"):
        await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)


@pytest.mark.asyncio
async def test_bookie_profits_when_opposite_side_wins(service):
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=1.5, no_odds=2.0)

    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)

    # Only YES money in pool; NO winning means no payouts.
    await svc.close_bet(bet_id)
    await svc.resolve_bet(bet_id, BetOutcome.NO)

    assert await db.get_balance(1, BOOKIE_ID) == STARTING_BALANCE + 100
    assert await db.get_balance(1, 50) == STARTING_BALANCE - 100
    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.escrow_balance == 0
    assert bet.bookie_reserve == 0


@pytest.mark.asyncio
async def test_bookie_loses_when_exposed_side_wins(service):
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=1.5, no_odds=2.0)

    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)

    await svc.close_bet(bet_id)
    await svc.resolve_bet(bet_id, BetOutcome.YES)

    assert await db.get_balance(1, 50) == STARTING_BALANCE - 100 + 150
    assert await db.get_balance(1, BOOKIE_ID) == STARTING_BALANCE - 50
    assert await _total_supply(db) == STARTING_BALANCE * 2


@pytest.mark.asyncio
async def test_balanced_market_bookie_collects_spread(service):
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=2.0, no_odds=2.0)

    await db.ensure_user(1, 50)
    await db.ensure_user(1, 51)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)
    await svc.place_or_update_wager(1, bet_id, 51, WagerPick.NO, 100)

    await svc.close_bet(bet_id)
    await svc.resolve_bet(bet_id, BetOutcome.YES)

    assert await db.get_balance(1, 50) == STARTING_BALANCE + 100
    assert await db.get_balance(1, 51) == STARTING_BALANCE - 100
    assert await db.get_balance(1, BOOKIE_ID) == STARTING_BALANCE


@pytest.mark.asyncio
async def test_bookie_can_go_negative_on_resolve(service):
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=2.0, no_odds=2.0)

    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)

    # Simulate reserve shortfall at settlement (e.g. manual adjustment / edge case).
    bet = await db.get_bet(bet_id)
    assert bet is not None
    await db.set_bet_escrow(bet_id, bet.escrow_balance, 0)
    await db.adjust_balance(1, BOOKIE_ID, bet.bookie_reserve)

    await svc.close_bet(bet_id)
    await svc.resolve_bet(bet_id, BetOutcome.YES)

    assert await db.get_balance(1, BOOKIE_ID) == STARTING_BALANCE - 100
    assert await db.get_balance(1, 50) == STARTING_BALANCE + 100


@pytest.mark.asyncio
async def test_cancel_returns_escrow_and_reserve(service):
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=1.5, no_odds=2.0)

    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)
    await svc.cancel_bet(bet_id)

    assert await db.get_balance(1, 50) == STARTING_BALANCE
    assert await db.get_balance(1, BOOKIE_ID) == STARTING_BALANCE
    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.status == BetStatus.CANCELLED
    assert bet.escrow_balance == 0


@pytest.mark.asyncio
async def test_remove_wager_updates_escrow_and_reserve(service):
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=1.5, no_odds=2.0)

    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)
    await svc.remove_wager_and_refund(1, bet_id, 50)

    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.escrow_balance == 0
    assert bet.bookie_reserve == 0
    assert await db.get_balance(1, BOOKIE_ID) == STARTING_BALANCE
    assert await db.get_balance(1, 50) == STARTING_BALANCE


@pytest.mark.asyncio
async def test_refund_outcome_returns_bettors_escrow(service):
    svc, db = service
    bet_id = await _open_bet(db)

    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 250)
    await svc.close_bet(bet_id)
    await svc.resolve_bet(bet_id, BetOutcome.REFUND)

    assert await db.get_balance(1, 50) == STARTING_BALANCE
    assert await db.get_balance(1, BOOKIE_ID) == STARTING_BALANCE


@pytest.mark.asyncio
async def test_update_wager_recomputes_reserve(service):
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=1.5, no_odds=2.0)

    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 200)

    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.escrow_balance == 200
    assert bet.bookie_reserve == 100
    assert await db.get_balance(1, BOOKIE_ID) == STARTING_BALANCE - 100

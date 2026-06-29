"""Tests for atomic cancellation, refunds, and transactional money integrity."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
import pytest

from bets import BetService
from config import STARTING_BALANCE
from database import Database
from models import BetStatus, WagerPick

BOOKIE_ID = 99
BETTOR_A = 50
BETTOR_B = 51


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


async def _open_bet(db: Database) -> int:
    close = datetime.now(timezone.utc) + timedelta(hours=2)
    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=BOOKIE_ID,
        question="Test?",
        close_time=close,
        yes_odds=2.0,
        no_odds=1.5,
    )
    return bet.id


async def _total_supply(db: Database, guild_id: int = 1) -> int:
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
async def test_concurrent_cancel_and_auto_refund_do_not_double_pay(service):
    """Only one of cancel_bet and refund_unresolved_bet may refund wagers."""
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_A)
    await db.ensure_user(1, BETTOR_B)
    await svc.place_or_update_wager(1, bet_id, BETTOR_A, WagerPick.YES, 100)
    await svc.place_or_update_wager(1, bet_id, BETTOR_B, WagerPick.NO, 80)
    await svc.close_bet(bet_id)

    supply_before = await _total_supply(db)

    results = await asyncio.gather(
        svc.cancel_bet(bet_id),
        svc.refund_unresolved_bet(bet_id),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception) and r is not None]
    failures = [r for r in results if isinstance(r, Exception) or r is None]

    assert len(successes) == 1, f"expected one success, got {results}"
    assert len(failures) == 1, f"expected one failure, got {results}"

    assert await db.get_balance(1, BETTOR_A) == STARTING_BALANCE
    assert await db.get_balance(1, BETTOR_B) == STARTING_BALANCE
    assert (await db.get_bet(bet_id)).status == BetStatus.CANCELLED
    assert await _total_supply(db) == supply_before


@pytest.mark.asyncio
async def test_concurrent_cancel_does_not_double_refund(service):
    """Two overlapping cancels must not refund wagers twice."""
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_A)
    await svc.place_or_update_wager(1, bet_id, BETTOR_A, WagerPick.YES, 100)

    supply_before = await _total_supply(db)

    results = await asyncio.gather(
        svc.cancel_bet(bet_id),
        svc.cancel_bet(bet_id),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception) and r is not None]
    failures = [r for r in results if isinstance(r, Exception) or r is None]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert await db.get_balance(1, BETTOR_A) == STARTING_BALANCE
    assert await _total_supply(db) == supply_before


@pytest.mark.asyncio
async def test_cancel_rolls_back_when_refund_fails_mid_transaction(service):
    """A failed mid-cancel refund must leave bet and balances unchanged."""
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_A)
    await db.ensure_user(1, BETTOR_B)
    await svc.place_or_update_wager(1, bet_id, BETTOR_A, WagerPick.YES, 100)
    await svc.place_or_update_wager(1, bet_id, BETTOR_B, WagerPick.NO, 50)

    supply_before = await _total_supply(db)
    balance_a_before = await db.get_balance(1, BETTOR_A)
    balance_b_before = await db.get_balance(1, BETTOR_B)

    original_adjust = db.adjust_balance
    calls = 0

    async def flaky_adjust(guild_id, user_id, delta, *, commit=True):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated refund failure")
        return await original_adjust(guild_id, user_id, delta, commit=commit)

    db.adjust_balance = flaky_adjust

    with pytest.raises(RuntimeError, match="simulated refund failure"):
        await svc.cancel_bet(bet_id)

    db.adjust_balance = original_adjust

    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.status == BetStatus.OPEN
    assert await db.get_wager(bet_id, BETTOR_A) is not None
    assert await db.get_wager(bet_id, BETTOR_B) is not None
    assert await db.get_balance(1, BETTOR_A) == balance_a_before
    assert await db.get_balance(1, BETTOR_B) == balance_b_before
    assert await _total_supply(db) == supply_before


@pytest.mark.asyncio
async def test_refund_unresolved_returns_none_when_already_cancelled(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await svc.close_bet(bet_id)
    await svc.cancel_bet(bet_id)
    assert await svc.refund_unresolved_bet(bet_id) is None


@pytest.mark.asyncio
async def test_claim_bet_for_cancellation_is_exclusive(service):
    db = service[1]
    bet_id = await _open_bet(db)

    first = await db.claim_bet_for_cancellation(bet_id)
    assert first is not None
    assert first.status == BetStatus.CANCELLED

    second = await db.claim_bet_for_cancellation(bet_id)
    assert second is None


@pytest.mark.asyncio
async def test_place_wager_transaction_rolls_back_on_escrow_failure(service):
    """Escrow update failure must not leave partial wager debits."""
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_A)
    await db.ensure_user(1, BOOKIE_ID)
    await db.adjust_balance(1, BOOKIE_ID, 5000)

    original_set_escrow = db.set_bet_escrow

    async def fail_escrow(*args, **kwargs):
        raise RuntimeError("escrow write failed")

    db.set_bet_escrow = fail_escrow

    with pytest.raises(RuntimeError, match="escrow write failed"):
        await svc.place_or_update_wager(1, bet_id, BETTOR_A, WagerPick.YES, 100)

    db.set_bet_escrow = original_set_escrow

    assert await db.get_wager(bet_id, BETTOR_A) is None
    assert await db.get_balance(1, BETTOR_A) == STARTING_BALANCE
    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.escrow_balance == 0
    assert bet.bookie_reserve == 0

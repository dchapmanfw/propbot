"""Edge-case tests for BetService."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from bets import BetService
from database import Database
from models import BetOutcome, BetStatus, WagerPick

BOOKIE = 99
BETTOR = 50


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


async def _open_bet(db: Database, **kwargs) -> int:
    close = datetime.now(timezone.utc) + timedelta(hours=kwargs.pop("hours", 2))
    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=BOOKIE,
        question="Test?",
        close_time=close,
        yes_odds=2.0,
        no_odds=1.5,
        **kwargs,
    )
    return bet.id


@pytest.mark.asyncio
async def test_place_wager_validation_errors(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR)

    with pytest.raises(ValueError, match="positive integer"):
        await svc.place_or_update_wager(1, bet_id, BETTOR, WagerPick.YES, 0)

    with pytest.raises(ValueError, match="Bet not found"):
        await svc.place_or_update_wager(1, 9999, BETTOR, WagerPick.YES, 10)

    with pytest.raises(ValueError, match="does not belong"):
        await svc.place_or_update_wager(2, bet_id, BETTOR, WagerPick.YES, 10)

    await db.update_bet_status(bet_id, BetStatus.CLOSED)
    with pytest.raises(ValueError, match="no longer open"):
        await svc.place_or_update_wager(1, bet_id, BETTOR, WagerPick.YES, 10)

    await db.update_bet_status(bet_id, BetStatus.OPEN)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db.conn.execute(
        "UPDATE bets SET close_time = ? WHERE id = ?", (past.isoformat(), bet_id)
    )
    await db.conn.commit()
    with pytest.raises(ValueError, match="already closed"):
        await svc.place_or_update_wager(1, bet_id, BETTOR, WagerPick.YES, 10)


@pytest.mark.asyncio
async def test_remove_wager_and_refund_noops(service):
    svc, db = service
    bet_id = await _open_bet(db)
    assert await svc.remove_wager_and_refund(1, bet_id, BETTOR) is None
    await db.update_bet_status(bet_id, BetStatus.CLOSED)
    await db.ensure_user(1, BETTOR)
    await db.upsert_wager(bet_id, BETTOR, WagerPick.YES, 10)
    assert await svc.remove_wager_and_refund(1, bet_id, BETTOR) is None


@pytest.mark.asyncio
async def test_close_bet_noops(service):
    svc, db = service
    assert await svc.close_bet(9999) is None
    bet_id = await _open_bet(db)
    await db.update_bet_status(bet_id, BetStatus.CLOSED)
    assert await svc.close_bet(bet_id) is None


@pytest.mark.asyncio
async def test_cancel_bet_errors(service):
    svc, db = service
    with pytest.raises(ValueError, match="Bet not found"):
        await svc.cancel_bet(9999)

    bet_id = await _open_bet(db)
    await db.update_bet_status(bet_id, BetStatus.RESOLVED, BetOutcome.YES)
    with pytest.raises(ValueError, match="cannot be cancelled"):
        await svc.cancel_bet(bet_id)


@pytest.mark.asyncio
async def test_resolve_bet_errors(service):
    svc, db = service
    with pytest.raises(ValueError, match="Bet not found"):
        await svc.resolve_bet(9999, BetOutcome.YES)

    bet_id = await _open_bet(db)
    await db.update_bet_status(bet_id, BetStatus.RESOLVED, BetOutcome.YES)
    with pytest.raises(ValueError, match="already resolved"):
        await svc.resolve_bet(bet_id, BetOutcome.YES)

    bet_id = await _open_bet(db)
    await db.update_bet_status(bet_id, BetStatus.CANCELLED)
    with pytest.raises(ValueError, match="was cancelled"):
        await svc.resolve_bet(bet_id, BetOutcome.YES)

    bet_id = await _open_bet(db)
    with pytest.raises(ValueError, match="must be closed"):
        await svc.resolve_bet(bet_id, BetOutcome.YES)


@pytest.mark.asyncio
async def test_refund_unresolved_releases_bookie_reserve(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR)
    await db.ensure_user(1, BOOKIE)
    await svc.place_or_update_wager(1, bet_id, BETTOR, WagerPick.YES, 100)
    bookie_before = await db.get_balance(1, BOOKIE)
    bet = await db.get_bet(bet_id)
    assert bet is not None
    reserve = bet.bookie_reserve
    await svc.close_bet(bet_id)
    result = await svc.refund_unresolved_bet(bet_id)
    assert result is not None
    assert result[1] == 1
    assert await db.get_balance(1, BOOKIE) == bookie_before + reserve


@pytest.mark.asyncio
async def test_refund_unresolved_returns_none_when_already_cancelled(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await svc.close_bet(bet_id)
    await svc.cancel_bet(bet_id)
    assert await svc.refund_unresolved_bet(bet_id) is None


@pytest.mark.asyncio
async def test_place_wager_rolls_back_escrow_on_upsert_failure(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR)
    await db.ensure_user(1, BOOKIE)
    await db.adjust_balance(1, BOOKIE, 5000)

    original_upsert = db.upsert_wager

    async def fail_upsert(*args, **kwargs):
        raise RuntimeError("upsert failed")

    db.upsert_wager = fail_upsert
    with pytest.raises(RuntimeError, match="upsert failed"):
        await svc.place_or_update_wager(1, bet_id, BETTOR, WagerPick.YES, 100)

    db.upsert_wager = original_upsert
    assert await db.get_wager(bet_id, BETTOR) is None
    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.escrow_balance == 0
    assert bet.bookie_reserve == 0

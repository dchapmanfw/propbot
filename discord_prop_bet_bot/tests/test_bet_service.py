"""Bet service integration tests."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from bets import BetService
from config import STARTING_BALANCE
from database import Database
from models import BetOutcome, BetStatus, WagerPick


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


async def _open_bet(db: Database, guild_id: int = 1) -> int:
    close = datetime.now(timezone.utc) + timedelta(hours=2)
    bet = await db.create_bet(
        guild_id=guild_id,
        channel_id=100,
        creator_id=99,
        question="Test bet?",
        close_time=close,
        yes_odds=2.0,
        no_odds=1.5,
    )
    return bet.id


@pytest.mark.asyncio
async def test_place_wager_deducts_balance(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 50)

    wager, balance = await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 200)
    assert wager.amount == 200
    assert balance == STARTING_BALANCE - 200


@pytest.mark.asyncio
async def test_insufficient_funds_rejected(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 50)

    with pytest.raises(ValueError, match="Insufficient balance"):
        await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 5000)


@pytest.mark.asyncio
async def test_creator_cannot_wager_on_own_bet(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 99)

    with pytest.raises(ValueError, match="cannot wager on a bet you created"):
        await svc.place_or_update_wager(1, bet_id, 99, WagerPick.YES, 100)

    assert await db.get_balance(1, 99) == STARTING_BALANCE
    assert await db.get_wager(bet_id, 99) is None


@pytest.mark.asyncio
async def test_update_wager_adjusts_balance(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 50)

    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)
    _, balance = await svc.place_or_update_wager(1, bet_id, 50, WagerPick.NO, 150)
    assert balance == STARTING_BALANCE - 150
    wager = await db.get_wager(bet_id, 50)
    assert wager is not None
    assert wager.pick == WagerPick.NO
    assert wager.amount == 150


@pytest.mark.asyncio
async def test_cancel_refunds_all_wagers(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 50)
    await db.ensure_user(1, 51)

    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)
    await svc.place_or_update_wager(1, bet_id, 51, WagerPick.NO, 200)
    await svc.cancel_bet(bet_id)

    assert await db.get_balance(1, 50) == STARTING_BALANCE
    assert await db.get_balance(1, 51) == STARTING_BALANCE
    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.status == BetStatus.CANCELLED


@pytest.mark.asyncio
async def test_resolve_pays_winners(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 50)
    await db.ensure_user(1, 51)

    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)
    await svc.place_or_update_wager(1, bet_id, 51, WagerPick.NO, 100)

    bet, payouts = await svc.resolve_bet(bet_id, BetOutcome.YES)
    assert bet.status == BetStatus.RESOLVED
    assert len(payouts) == 1
    assert payouts[0][1] == 200  # 100 * 2.0
    assert await db.get_balance(1, 50) == STARTING_BALANCE - 100 + 200
    assert await db.get_balance(1, 51) == STARTING_BALANCE - 100


@pytest.mark.asyncio
async def test_resolve_refund_returns_all_wagers(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 50)

    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 250)
    await svc.resolve_bet(bet_id, BetOutcome.REFUND)

    assert await db.get_balance(1, 50) == STARTING_BALANCE


@pytest.mark.asyncio
async def test_remove_wager_refunds_on_reaction_remove(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 80)

    refunded = await svc.remove_wager_and_refund(1, bet_id, 50)
    assert refunded == 80
    assert await db.get_balance(1, 50) == STARTING_BALANCE
    assert await db.get_wager(bet_id, 50) is None


@pytest.mark.asyncio
async def test_close_bet_changes_status(service):
    svc, db = service
    bet_id = await _open_bet(db)
    closed = await svc.close_bet(bet_id)
    assert closed is not None
    assert closed.status == BetStatus.CLOSED


@pytest.mark.asyncio
async def test_refund_unresolved_bet_returns_wagers(service):
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, 50)
    await db.ensure_user(1, 51)

    await svc.place_or_update_wager(1, bet_id, 50, WagerPick.YES, 100)
    await svc.place_or_update_wager(1, bet_id, 51, WagerPick.NO, 200)
    await svc.close_bet(bet_id)

    result = await svc.refund_unresolved_bet(bet_id)
    assert result is not None
    bet, count = result
    assert bet.status == BetStatus.CANCELLED
    assert count == 2
    assert await db.get_balance(1, 50) == STARTING_BALANCE
    assert await db.get_balance(1, 51) == STARTING_BALANCE
    assert await db.get_wagers_for_bet(bet_id) == []


@pytest.mark.asyncio
async def test_refund_unresolved_bet_ignores_open_bets(service):
    svc, db = service
    bet_id = await _open_bet(db)
    assert await svc.refund_unresolved_bet(bet_id) is None


@pytest.mark.asyncio
async def test_get_stale_closed_bets(service):
    svc, db = service
    past = datetime.now(timezone.utc) - timedelta(hours=48)
    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Old closed bet?",
        close_time=past,
        yes_odds=2.0,
        no_odds=1.5,
    )
    await db.update_bet_status(bet.id, BetStatus.CLOSED)

    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Recently closed bet?",
        close_time=recent,
        yes_odds=2.0,
        no_odds=1.5,
    )
    await db.update_bet_status(recent_bet.id, BetStatus.CLOSED)

    stale = await db.get_stale_closed_bets(timedelta(hours=24))
    stale_ids = {b.id for b in stale}
    assert bet.id in stale_ids
    assert recent_bet.id not in stale_ids

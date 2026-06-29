"""
Regression tests for issues identified in code review.

These encode the *correct* expected behavior. They should fail until the
underlying bugs are fixed.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import channel_policy as cp
from bets import BetService
from bot import INTENTS
from commands import PropBetCommands
from config import NO_EMOJI, STARTING_BALANCE, YES_EMOJI
from database import Database
from models import BetOutcome, BetStatus, WagerPick

BOOKIE_ID = 99
BETTOR_ID = 50


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
    hours_until_close: float = 2,
) -> int:
    close = datetime.now(timezone.utc) + timedelta(hours=hours_until_close)
    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=BOOKIE_ID,
        question="Test bet?",
        close_time=close,
        yes_odds=yes_odds,
        no_odds=no_odds,
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


# --- Issue 1: reaction remove must match wager pick ---


@pytest.fixture
async def reaction_cog(monkeypatch):
    """PropBetCommands wired to a real DB and minimal bot mock."""
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", None)

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.connect()

    bot = MagicMock()
    bot.db = db
    bot.user = MagicMock()
    bot.user.id = 1
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(return_value=MagicMock())
    bot.refresh_bet_message = AsyncMock()
    cog = PropBetCommands(bot)

    yield cog, db
    await db.close()
    os.unlink(path)


@pytest.mark.asyncio
async def test_reaction_remove_unmatched_emoji_does_not_refund(reaction_cog):
    """Removing the opposite emoji must not cancel an active wager."""
    cog, db = reaction_cog
    svc = BetService(db)
    bet_id = await _open_bet(db)
    await db.set_bet_message_id(bet_id, 4242)
    await db.ensure_user(1, BETTOR_ID)

    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 100)
    balance_before = await db.get_balance(1, BETTOR_ID)

    payload = SimpleNamespace(
        user_id=BETTOR_ID,
        channel_id=100,
        message_id=4242,
        emoji=NO_EMOJI,
    )
    cog._notify_user = AsyncMock()
    await cog.on_raw_reaction_remove(payload)

    wager = await db.get_wager(bet_id, BETTOR_ID)
    assert wager is not None, "wager should remain after removing unrelated emoji"
    assert wager.pick == WagerPick.YES
    assert wager.amount == 100
    assert await db.get_balance(1, BETTOR_ID) == balance_before


@pytest.mark.asyncio
async def test_reaction_remove_matching_emoji_still_refunds(reaction_cog):
    """Sanity check: removing the wagered emoji should refund."""
    cog, db = reaction_cog
    svc = BetService(db)
    bet_id = await _open_bet(db)
    await db.set_bet_message_id(bet_id, 4242)
    await db.ensure_user(1, BETTOR_ID)

    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 100)

    payload = SimpleNamespace(
        user_id=BETTOR_ID,
        channel_id=100,
        message_id=4242,
        emoji=YES_EMOJI,
    )
    cog._notify_user = AsyncMock()
    await cog.on_raw_reaction_remove(payload)

    assert await db.get_wager(bet_id, BETTOR_ID) is None
    assert await db.get_balance(1, BETTOR_ID) == STARTING_BALANCE


# --- Issue 5: wager-update rollback must not mint coins ---


@pytest.mark.asyncio
async def test_wager_update_failure_restores_bettor_balance(service):
    """If escrow update fails mid-update, bettor balance must match pre-update state."""
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_ID)

    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 100)
    balance_after_first = await db.get_balance(1, BETTOR_ID)

    original_upsert = db.upsert_wager
    fail_upsert = False

    async def flaky_upsert(*args, **kwargs):
        if fail_upsert:
            raise RuntimeError("simulated upsert failure")
        return await original_upsert(*args, **kwargs)

    db.upsert_wager = flaky_upsert
    fail_upsert = True

    with pytest.raises(RuntimeError, match="simulated upsert failure"):
        await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 200)

    assert await db.get_balance(1, BETTOR_ID) == balance_after_first
    wager = await db.get_wager(bet_id, BETTOR_ID)
    assert wager is not None
    assert wager.amount == 100
    assert wager.pick == WagerPick.YES


@pytest.mark.asyncio
async def test_wager_update_failure_restores_bookie_reserve(service):
    """If escrow update fails, bookie reserve lock must be rolled back."""
    svc, db = service
    bet_id = await _open_bet(db, yes_odds=1.5, no_odds=2.0)
    await db.ensure_user(1, BETTOR_ID)
    await db.ensure_user(1, BOOKIE_ID)

    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 100)
    bookie_after_first_wager = await db.get_balance(1, BOOKIE_ID)
    assert bookie_after_first_wager == STARTING_BALANCE - 50

    original_set_escrow = db.set_bet_escrow
    fail_escrow = False

    async def flaky_set_escrow(*args, **kwargs):
        if fail_escrow:
            raise RuntimeError("simulated escrow failure")
        return await original_set_escrow(*args, **kwargs)

    db.set_bet_escrow = flaky_set_escrow
    fail_escrow = True

    with pytest.raises(RuntimeError, match="simulated escrow failure"):
        await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 200)

    assert await db.get_balance(1, BOOKIE_ID) == bookie_after_first_wager
    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.bookie_reserve == 50


# --- Issue 4: concurrent resolve must not double-pay ---


@pytest.mark.asyncio
async def test_concurrent_resolve_does_not_double_pay_winners(service):
    """Two overlapping resolves must not pay winners twice."""
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_ID)
    await db.ensure_user(1, BOOKIE_ID)

    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 100)
    await svc.close_bet(bet_id)

    supply_before = await _total_supply(db)

    results = await asyncio.gather(
        svc.resolve_bet(bet_id, BetOutcome.YES),
        svc.resolve_bet(bet_id, BetOutcome.YES),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception) and r is not None]
    failures = [r for r in results if isinstance(r, Exception) or r is None]

    # Exactly one resolve should succeed; the other must fail cleanly.
    assert len(successes) == 1, f"expected one success, got {results}"
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)

    expected_winner_balance = STARTING_BALANCE - 100 + 200
    assert await db.get_balance(1, BETTOR_ID) == expected_winner_balance
    assert await _total_supply(db) == supply_before


# --- Issue 7: resolve should require bet to be closed first ---


@pytest.mark.asyncio
async def test_cannot_resolve_open_bet_before_close_time(service):
    """OPEN bets still inside their window must not be resolvable."""
    svc, db = service
    bet_id = await _open_bet(db, hours_until_close=2)
    await db.ensure_user(1, BETTOR_ID)

    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 100)
    bet = await db.get_bet(bet_id)
    assert bet is not None
    assert bet.status == BetStatus.OPEN

    with pytest.raises(ValueError, match="(?i)closed|open"):
        await svc.resolve_bet(bet_id, BetOutcome.YES)

    assert (await db.get_bet(bet_id)).status == BetStatus.OPEN


# --- Issue 8: concurrent cancel / auto-refund must not double-refund ---


@pytest.mark.asyncio
async def test_concurrent_cancel_and_refund_unresolved(service):
    """cancel_bet and refund_unresolved_bet must not both succeed on a closed bet."""
    svc, db = service
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_ID)
    await db.ensure_user(1, BOOKIE_ID)
    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 100)
    await svc.close_bet(bet_id)

    supply_before = await _total_supply(db)

    results = await asyncio.gather(
        svc.cancel_bet(bet_id),
        svc.refund_unresolved_bet(bet_id),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception) and r is not None]
    failures = [r for r in results if isinstance(r, Exception) or r is None]

    assert len(successes) == 1
    assert len(failures) == 1
    assert await db.get_balance(1, BETTOR_ID) == STARTING_BALANCE
    assert await _total_supply(db) == supply_before


# --- Issue 6: unnecessary privileged intent ---


def test_message_content_intent_is_not_enabled():
    """message_content is unused; enabling it widens permission scope unnecessarily."""
    assert INTENTS.message_content is False

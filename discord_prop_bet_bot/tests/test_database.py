"""Async database tests."""

import os
import tempfile

import pytest

from config import STARTING_BALANCE
from database import Database
from models import BetStatus, WagerPick


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    await database.connect()
    yield database
    await database.close()
    os.unlink(path)


@pytest.mark.asyncio
async def test_ensure_user_starts_with_default_balance(db: Database):
    user = await db.ensure_user(guild_id=1, user_id=42)
    assert user.balance == STARTING_BALANCE

    again = await db.ensure_user(guild_id=1, user_id=42)
    assert again.balance == STARTING_BALANCE


@pytest.mark.asyncio
async def test_adjust_balance_prevents_negative(db: Database):
    await db.ensure_user(1, 1)
    with pytest.raises(ValueError, match="Insufficient balance"):
        await db.adjust_balance(1, 1, -5000)


@pytest.mark.asyncio
async def test_create_bet_and_wager(db: Database):
    from datetime import datetime, timedelta, timezone

    await db.ensure_user(1, 10)
    close = datetime.now(timezone.utc) + timedelta(hours=1)
    bet = await db.create_bet(
        guild_id=1,
        channel_id=2,
        creator_id=10,
        question="Will it rain?",
        close_time=close,
        yes_odds=1.5,
        no_odds=2.0,
    )
    assert bet.status == BetStatus.OPEN
    await db.set_bet_message_id(bet.id, 999)
    found = await db.get_bet_by_message(999)
    assert found is not None
    assert found.id == bet.id

    await db.adjust_balance(1, 20, -100)
    await db.upsert_wager(bet.id, 20, WagerPick.YES, 100)
    wagers = await db.get_wagers_for_bet(bet.id)
    assert len(wagers) == 1
    assert wagers[0].amount == 100


@pytest.mark.asyncio
async def test_leaderboard_ordering(db: Database):
    await db.ensure_user(1, 1)
    await db.ensure_user(1, 2)
    await db.adjust_balance(1, 1, 500)
    await db.adjust_balance(1, 2, -200)

    board = await db.get_leaderboard(1)
    assert board[0].user_id == 1
    assert board[0].balance == STARTING_BALANCE + 500
    assert board[1].user_id == 2

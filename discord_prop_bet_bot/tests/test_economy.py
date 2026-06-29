"""Tests for balance reset and anti-prestige redemption."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from config import STARTING_BALANCE
from database import Database
from economy import REDEMPTION_COST, EconomyService
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


@pytest.fixture
def economy(db):
    return EconomyService(db)


@pytest.mark.asyncio
async def test_new_user_has_zero_reset_count(db: Database):
    user = await db.ensure_user(1, 42)
    assert user.reset_count == 0


@pytest.mark.asyncio
async def test_reset_sets_starting_balance_and_increments_count(economy: EconomyService, db: Database):
    await db.ensure_user(1, 10)
    await db.adjust_balance_allow_negative(1, 10, -1500)

    user = await economy.reset_balance(1, 10)
    assert user.balance == STARTING_BALANCE
    assert user.reset_count == 1


@pytest.mark.asyncio
async def test_reset_rejected_at_or_above_starting_balance(economy: EconomyService, db: Database):
    await db.ensure_user(1, 10)
    with pytest.raises(ValueError, match="below"):
        await economy.reset_balance(1, 10)

    await db.adjust_balance(1, 10, 500)
    with pytest.raises(ValueError, match="below"):
        await economy.reset_balance(1, 10)


@pytest.mark.asyncio
async def test_reset_blocked_with_open_wager(economy: EconomyService, db: Database):
    await db.ensure_user(1, 10)
    await db.ensure_user(1, 20)
    await db.adjust_balance(1, 20, -500)
    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=10,
        question="?",
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
        yes_odds=2.0,
        no_odds=2.0,
    )
    await db.upsert_wager(bet.id, 20, WagerPick.YES, 100)

    with pytest.raises(ValueError, match="active bets"):
        await economy.reset_balance(1, 20)


@pytest.mark.asyncio
async def test_reset_blocked_for_bookie_with_open_bet(economy: EconomyService, db: Database):
    await db.ensure_user(1, 10)
    await db.adjust_balance_allow_negative(1, 10, -500)
    await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=10,
        question="?",
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
        yes_odds=2.0,
        no_odds=2.0,
    )

    with pytest.raises(ValueError, match="active bets"):
        await economy.reset_balance(1, 10)


@pytest.mark.asyncio
async def test_redeem_cost_and_clears_one_reset(economy: EconomyService, db: Database):
    await db.ensure_user(1, 10)
    await db.adjust_balance_allow_negative(1, 10, -1500)
    await economy.reset_balance(1, 10)
    await db.adjust_balance(1, 10, REDEMPTION_COST)

    user = await economy.redeem_reset(1, 10)
    assert user.reset_count == 0
    assert user.balance == STARTING_BALANCE


@pytest.mark.asyncio
async def test_redeem_rejected_without_reset_count(economy: EconomyService, db: Database):
    await db.ensure_user(1, 10)
    with pytest.raises(ValueError, match="no anti-prestige"):
        await economy.redeem_reset(1, 10)


@pytest.mark.asyncio
async def test_redeem_rejected_insufficient_balance(economy: EconomyService, db: Database):
    await db.ensure_user(1, 10)
    await db.adjust_balance_allow_negative(1, 10, -1500)
    await economy.reset_balance(1, 10)

    with pytest.raises(ValueError, match="Redemption costs"):
        await economy.redeem_reset(1, 10)


@pytest.mark.asyncio
async def test_leaderboard_ranks_clean_players_above_resetters(db: Database):
    await db.ensure_user(1, 1)
    await db.ensure_user(1, 2)
    await db.adjust_balance(1, 1, -900)  # 100 coins, clean
    await db.reset_user_balance(1, 2)  # 1000 coins, reset_count 1

    board = await db.get_leaderboard(1)
    assert board[0].user_id == 1
    assert board[1].user_id == 2
    assert board[1].reset_count == 1


@pytest.mark.asyncio
async def test_leaderboard_among_resetters_fewer_resets_rank_higher(db: Database):
    await db.ensure_user(1, 1)
    await db.ensure_user(1, 2)
    await db.reset_user_balance(1, 1)
    await db.reset_user_balance(1, 2)
    await db.reset_user_balance(1, 2)
    await db.adjust_balance(1, 2, 5000)

    board = await db.get_leaderboard(1)
    assert board[0].user_id == 1
    assert board[0].reset_count == 1
    assert board[1].user_id == 2
    assert board[1].reset_count == 2

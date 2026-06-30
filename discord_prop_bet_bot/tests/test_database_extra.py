"""Additional database layer tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from database import Database
from markets import MarketService
from models import BetOutcome, BetStatus, WagerPick


@pytest.mark.asyncio
async def test_conn_raises_when_not_connected():
    db = Database(":memory:")
    with pytest.raises(RuntimeError, match="not connected"):
        _ = db.conn


@pytest.mark.asyncio
async def test_migrate_schema_adds_missing_columns():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER,
            creator_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            close_time TEXT NOT NULL,
            yes_odds REAL NOT NULL,
            no_odds REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            outcome TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    await db.connect()
    cursor = await db.conn.execute("PRAGMA table_info(bets)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "escrow_balance" in columns
    assert "bookie_reserve" in columns
    await db.close()
    os.unlink(path)


@pytest.mark.asyncio
async def test_get_expired_open_bets():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.connect()

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    expired = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Expired?",
        close_time=past,
        yes_odds=2.0,
        no_odds=1.5,
    )
    await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Future?",
        close_time=future,
        yes_odds=2.0,
        no_odds=1.5,
    )

    expired_bets = await db.get_expired_open_bets(now=datetime.now(timezone.utc))
    assert [b.id for b in expired_bets] == [expired.id]
    await db.close()
    os.unlink(path)


@pytest.mark.asyncio
async def test_remove_wager_returns_none_when_missing():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.connect()
    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="?",
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
        yes_odds=2.0,
        no_odds=1.5,
    )
    assert await db.remove_wager(bet.id, 12345) is None
    await db.close()
    os.unlink(path)


@pytest.mark.asyncio
async def test_get_user_bets_and_leaderboard():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.connect()

    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Mine?",
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
        yes_odds=2.0,
        no_odds=1.5,
    )
    await db.ensure_user(1, 50)
    await db.upsert_wager(bet.id, 50, WagerPick.YES, 25)

    user_bets = await db.get_user_bets(1, 50)
    assert len(user_bets) == 1
    creator_bets = await db.get_user_bets(1, 99)
    assert len(creator_bets) == 1

    await db.ensure_user(1, 99)
    board = await db.get_leaderboard(1, limit=5)
    assert len(board) == 2
    await db.close()
    os.unlink(path)


@pytest.mark.asyncio
async def test_get_unresolved_markets():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.connect()

    close = datetime.now(timezone.utc) + timedelta(hours=2)
    service = MarketService(db)
    open_market = await service.create_market(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Open market?",
        close_time=close,
    )
    closed_market = await service.create_market(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Closed market?",
        close_time=close,
    )
    await db.update_bet_status(closed_market.id, BetStatus.CLOSED)
    resolved_market = await service.create_market(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Resolved market?",
        close_time=close,
    )
    await db.update_bet_status(resolved_market.id, BetStatus.RESOLVED, BetOutcome.YES)
    await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Prop bet?",
        close_time=close,
        yes_odds=2.0,
        no_odds=1.5,
    )

    markets = await db.get_unresolved_markets(1)
    assert [m.id for m in markets] == [open_market.id, closed_market.id]
    assert all(m.bet_kind.value == "market" for m in markets)
    assert await db.get_unresolved_markets(2) == []

    await db.close()
    os.unlink(path)

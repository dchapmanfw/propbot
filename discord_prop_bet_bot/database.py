"""Async SQLite persistence layer."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from config import DATABASE_PATH, STARTING_BALANCE
from models import Bet, BetOutcome, BetStatus, UserBalance, Wager, WagerPick

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    balance INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS bets (
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
);

CREATE TABLE IF NOT EXISTS wagers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    pick TEXT NOT NULL,
    amount INTEGER NOT NULL,
    FOREIGN KEY (bet_id) REFERENCES bets(id) ON DELETE CASCADE,
    UNIQUE (bet_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS idx_bets_guild ON bets(guild_id);
CREATE INDEX IF NOT EXISTS idx_wagers_bet ON wagers(bet_id);
"""


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_bet(row: aiosqlite.Row) -> Bet:
    return Bet(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        creator_id=row["creator_id"],
        question=row["question"],
        close_time=_parse_dt(row["close_time"]),
        yes_odds=row["yes_odds"],
        no_odds=row["no_odds"],
        status=BetStatus(row["status"]),
        outcome=BetOutcome(row["outcome"]) if row["outcome"] else None,
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_wager(row: aiosqlite.Row) -> Wager:
    return Wager(
        id=row["id"],
        bet_id=row["bet_id"],
        user_id=row["user_id"],
        pick=WagerPick(row["pick"]),
        amount=row["amount"],
    )


class Database:
    """Thin async wrapper around SQLite for prop bet storage."""

    def __init__(self, path: str = DATABASE_PATH) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info("Database initialized at %s", self.path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    async def ensure_user(self, guild_id: int, user_id: int) -> UserBalance:
        """Create a user with starting balance if they do not exist yet."""
        now_balance = await self.get_balance(guild_id, user_id)
        if now_balance is not None:
            return UserBalance(guild_id, user_id, now_balance)

        await self.conn.execute(
            "INSERT INTO users (guild_id, user_id, balance) VALUES (?, ?, ?)",
            (guild_id, user_id, STARTING_BALANCE),
        )
        await self.conn.commit()
        return UserBalance(guild_id, user_id, STARTING_BALANCE)

    async def get_balance(self, guild_id: int, user_id: int) -> int | None:
        cursor = await self.conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        return int(row["balance"]) if row else None

    async def adjust_balance(self, guild_id: int, user_id: int, delta: int) -> int:
        """Atomically adjust balance. Raises ValueError if result would be negative."""
        await self.ensure_user(guild_id, user_id)
        cursor = await self.conn.execute(
            """
            UPDATE users
            SET balance = balance + ?
            WHERE guild_id = ? AND user_id = ? AND balance + ? >= 0
            """,
            (delta, guild_id, user_id, delta),
        )
        if cursor.rowcount == 0:
            raise ValueError("Insufficient balance")
        await self.conn.commit()
        balance = await self.get_balance(guild_id, user_id)
        assert balance is not None
        return balance

    async def create_bet(
        self,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        question: str,
        close_time: datetime,
        yes_odds: float,
        no_odds: float,
    ) -> Bet:
        now = datetime.now(timezone.utc)
        cursor = await self.conn.execute(
            """
            INSERT INTO bets (
                guild_id, channel_id, creator_id, question,
                close_time, yes_odds, no_odds, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                creator_id,
                question,
                close_time.isoformat(),
                yes_odds,
                no_odds,
                BetStatus.OPEN.value,
                now.isoformat(),
            ),
        )
        await self.conn.commit()
        bet_id = cursor.lastrowid
        bet = await self.get_bet(bet_id)
        assert bet is not None
        return bet

    async def set_bet_message_id(self, bet_id: int, message_id: int) -> None:
        await self.conn.execute(
            "UPDATE bets SET message_id = ? WHERE id = ?",
            (message_id, bet_id),
        )
        await self.conn.commit()

    async def get_bet(self, bet_id: int) -> Bet | None:
        cursor = await self.conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,))
        row = await cursor.fetchone()
        return _row_to_bet(row) if row else None

    async def get_bet_by_message(self, message_id: int) -> Bet | None:
        cursor = await self.conn.execute(
            "SELECT * FROM bets WHERE message_id = ?", (message_id,)
        )
        row = await cursor.fetchone()
        return _row_to_bet(row) if row else None

    async def update_bet_status(
        self,
        bet_id: int,
        status: BetStatus,
        outcome: BetOutcome | None = None,
    ) -> Bet | None:
        await self.conn.execute(
            "UPDATE bets SET status = ?, outcome = ? WHERE id = ?",
            (status.value, outcome.value if outcome else None, bet_id),
        )
        await self.conn.commit()
        return await self.get_bet(bet_id)

    async def get_open_bets(self) -> list[Bet]:
        cursor = await self.conn.execute(
            "SELECT * FROM bets WHERE status = ? ORDER BY close_time ASC",
            (BetStatus.OPEN.value,),
        )
        rows = await cursor.fetchall()
        return [_row_to_bet(row) for row in rows]

    async def get_expired_open_bets(self, now: datetime | None = None) -> list[Bet]:
        now = now or datetime.now(timezone.utc)
        cursor = await self.conn.execute(
            """
            SELECT * FROM bets
            WHERE status = ? AND close_time <= ?
            ORDER BY close_time ASC
            """,
            (BetStatus.OPEN.value, now.isoformat()),
        )
        rows = await cursor.fetchall()
        return [_row_to_bet(row) for row in rows]

    async def get_stale_closed_bets(
        self,
        refund_after: timedelta,
        now: datetime | None = None,
    ) -> list[Bet]:
        """Closed bets past close_time + refund_after that still await resolution."""
        now = now or datetime.now(timezone.utc)
        refund_cutoff = now - refund_after
        cursor = await self.conn.execute(
            """
            SELECT * FROM bets
            WHERE status = ? AND close_time <= ?
            ORDER BY close_time ASC
            """,
            (BetStatus.CLOSED.value, refund_cutoff.isoformat()),
        )
        rows = await cursor.fetchall()
        return [_row_to_bet(row) for row in rows]

    async def get_wager(self, bet_id: int, user_id: int) -> Wager | None:
        cursor = await self.conn.execute(
            "SELECT * FROM wagers WHERE bet_id = ? AND user_id = ?",
            (bet_id, user_id),
        )
        row = await cursor.fetchone()
        return _row_to_wager(row) if row else None

    async def get_wagers_for_bet(self, bet_id: int) -> list[Wager]:
        cursor = await self.conn.execute(
            "SELECT * FROM wagers WHERE bet_id = ? ORDER BY amount DESC",
            (bet_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_wager(row) for row in rows]

    async def upsert_wager(
        self,
        bet_id: int,
        user_id: int,
        pick: WagerPick,
        amount: int,
    ) -> Wager:
        """Replace a user's wager on a bet (used after balance adjustment in service layer)."""
        await self.conn.execute(
            """
            INSERT INTO wagers (bet_id, user_id, pick, amount)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(bet_id, user_id) DO UPDATE SET
                pick = excluded.pick,
                amount = excluded.amount
            """,
            (bet_id, user_id, pick.value, amount),
        )
        await self.conn.commit()
        wager = await self.get_wager(bet_id, user_id)
        assert wager is not None
        return wager

    async def remove_wager(self, bet_id: int, user_id: int) -> Wager | None:
        wager = await self.get_wager(bet_id, user_id)
        if not wager:
            return None
        await self.conn.execute(
            "DELETE FROM wagers WHERE bet_id = ? AND user_id = ?",
            (bet_id, user_id),
        )
        await self.conn.commit()
        return wager

    async def get_user_bets(
        self,
        guild_id: int,
        user_id: int,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Bets the user created or wagered on, most recent first."""
        cursor = await self.conn.execute(
            """
            SELECT DISTINCT b.*
            FROM bets b
            LEFT JOIN wagers w ON w.bet_id = b.id
            WHERE b.guild_id = ? AND (b.creator_id = ? OR w.user_id = ?)
            ORDER BY b.created_at DESC
            LIMIT ?
            """,
            (guild_id, user_id, user_id, limit),
        )
        rows = await cursor.fetchall()
        return [_row_to_bet(row) for row in rows]

    async def get_leaderboard(self, guild_id: int, limit: int = 10) -> list[UserBalance]:
        cursor = await self.conn.execute(
            """
            SELECT guild_id, user_id, balance
            FROM users
            WHERE guild_id = ?
            ORDER BY balance DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            UserBalance(row["guild_id"], row["user_id"], row["balance"]) for row in rows
        ]

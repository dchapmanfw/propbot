"""Data models for the prop bet bot."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class BetStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class BetOutcome(str, Enum):
    YES = "yes"
    NO = "no"
    REFUND = "refund"  # Tie / N/A — refund all wagers


class WagerPick(str, Enum):
    YES = "yes"
    NO = "no"


class BetKind(str, Enum):
    PROP = "prop"
    MARKET = "market"


@dataclass
class UserBalance:
    guild_id: int
    user_id: int
    balance: int
    reset_count: int = 0
    portfolio_value: int = 0

    @property
    def total_value(self) -> int:
        """Liquid balance plus mark-to-market value of open market holdings."""
        return self.balance + self.portfolio_value


@dataclass
class Bet:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    creator_id: int
    question: str
    close_time: datetime
    yes_odds: float
    no_odds: float
    status: BetStatus
    outcome: BetOutcome | None
    created_at: datetime
    escrow_balance: int = 0
    bookie_reserve: int = 0
    bet_kind: BetKind = BetKind.PROP
    q_yes: float = 0.0
    q_no: float = 0.0
    liquidity_b: float = 100.0
    board_message_id: int | None = None
    resolved_at: datetime | None = None


class MarketSnapshotEvent(str, Enum):
    OPEN = "open"
    BUY = "buy"
    SELL = "sell"


@dataclass
class MarketPosition:
    id: int
    bet_id: int
    user_id: int
    side: WagerPick
    shares: float


@dataclass
class MarketPriceSnapshot:
    id: int
    bet_id: int
    recorded_at: datetime
    q_yes: float
    q_no: float
    yes_price: float
    event: MarketSnapshotEvent


@dataclass
class Wager:
    id: int
    bet_id: int
    user_id: int
    pick: WagerPick
    amount: int

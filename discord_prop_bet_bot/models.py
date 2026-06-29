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


@dataclass
class MarketPosition:
    id: int
    bet_id: int
    user_id: int
    side: WagerPick
    shares: float


@dataclass
class Wager:
    id: int
    bet_id: int
    user_id: int
    pick: WagerPick
    amount: int

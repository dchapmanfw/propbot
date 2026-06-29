"""Tests for payout calculations."""

from datetime import datetime, timezone

from bets import compute_payout
from models import Bet, BetOutcome, BetStatus, Wager, WagerPick


def _sample_bet() -> Bet:
    now = datetime.now(timezone.utc)
    return Bet(
        id=1,
        guild_id=1,
        channel_id=1,
        message_id=1,
        creator_id=99,
        question="Test?",
        close_time=now,
        yes_odds=1.5,
        no_odds=2.0,
        status=BetStatus.OPEN,
        outcome=None,
        created_at=now,
    )


def test_yes_winner_payout():
    bet = _sample_bet()
    wager = Wager(id=1, bet_id=1, user_id=2, pick=WagerPick.YES, amount=100)
    assert compute_payout(wager, bet, BetOutcome.YES) == 150


def test_no_winner_payout():
    bet = _sample_bet()
    wager = Wager(id=1, bet_id=1, user_id=2, pick=WagerPick.NO, amount=50)
    assert compute_payout(wager, bet, BetOutcome.NO) == 100


def test_loser_gets_zero():
    bet = _sample_bet()
    wager = Wager(id=1, bet_id=1, user_id=2, pick=WagerPick.YES, amount=100)
    assert compute_payout(wager, bet, BetOutcome.NO) == 0


def test_refund_returns_wager():
    bet = _sample_bet()
    wager = Wager(id=1, bet_id=1, user_id=2, pick=WagerPick.NO, amount=75)
    assert compute_payout(wager, bet, BetOutcome.REFUND) == 75

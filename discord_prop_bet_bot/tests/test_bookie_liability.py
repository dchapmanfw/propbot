"""Tests for bookie reserve / escrow liability calculations."""

from bets import compute_bookie_max_additional_wager, compute_bookie_reserve
from models import Bet, BetStatus, Wager, WagerPick
from datetime import datetime, timezone


def _wager(user_id: int, pick: WagerPick, amount: int) -> Wager:
    return Wager(id=0, bet_id=1, user_id=user_id, pick=pick, amount=amount)


def _bet(**kwargs) -> Bet:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=1,
        guild_id=1,
        channel_id=100,
        message_id=999,
        creator_id=99,
        question="?",
        close_time=now,
        yes_odds=2.0,
        no_odds=2.0,
        status=BetStatus.OPEN,
        outcome=None,
        created_at=now,
        escrow_balance=0,
        bookie_reserve=0,
    )
    defaults.update(kwargs)
    return Bet(**defaults)


def test_balanced_pool_no_reserve_needed_at_2x():
    wagers = [_wager(1, WagerPick.YES, 100), _wager(2, WagerPick.NO, 100)]
    pool, yes_pay, no_pay, reserve = compute_bookie_reserve(wagers, 2.0, 2.0)
    assert pool == 200
    assert yes_pay == 200
    assert no_pay == 200
    assert reserve == 0


def test_one_sided_yes_needs_reserve():
    wagers = [_wager(1, WagerPick.YES, 100)]
    pool, yes_pay, no_pay, reserve = compute_bookie_reserve(wagers, 1.5, 2.0)
    assert pool == 100
    assert yes_pay == 150
    assert no_pay == 0
    assert reserve == 50


def test_skewed_pool_reserve_is_worst_case():
    wagers = [_wager(1, WagerPick.YES, 300), _wager(2, WagerPick.NO, 100)]
    pool, _, _, reserve = compute_bookie_reserve(wagers, 2.0, 2.0)
    assert pool == 400
    # YES wins: pay 600, shortfall 200. NO wins: pay 200, surplus.
    assert reserve == 200


def test_bookie_max_additional_wager_empty_pool():
    bet = _bet()
    assert compute_bookie_max_additional_wager(bet, [], WagerPick.YES, 1000) == 1000


def test_bookie_max_additional_wager_balanced_pool_capped_by_bookie():
    bet = _bet(escrow_balance=200, bookie_reserve=0)
    wagers = [_wager(1, WagerPick.YES, 100), _wager(2, WagerPick.NO, 100)]
    assert compute_bookie_max_additional_wager(bet, wagers, WagerPick.YES, 1000) == 1000


def test_bookie_max_additional_wager_unlimited_when_no_reserve_growth():
    bet = _bet(yes_odds=1.0, no_odds=1.0)
    assert compute_bookie_max_additional_wager(bet, [], WagerPick.YES, 100) is None


def test_bookie_max_additional_wager_zero_when_bookie_broke():
    bet = _bet(escrow_balance=100, bookie_reserve=50)
    wagers = [_wager(1, WagerPick.YES, 100)]
    assert compute_bookie_max_additional_wager(bet, wagers, WagerPick.YES, 0) == 0

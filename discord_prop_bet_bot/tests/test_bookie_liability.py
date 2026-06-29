"""Tests for bookie reserve / escrow liability calculations."""

from bets import compute_bookie_reserve
from models import Wager, WagerPick


def _wager(user_id: int, pick: WagerPick, amount: int) -> Wager:
    return Wager(id=0, bet_id=1, user_id=user_id, pick=pick, amount=amount)


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

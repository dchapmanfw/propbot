"""Tests for LMSR pricing helpers."""

from __future__ import annotations

import math

import pytest

from lmsr import (
    format_price_cents,
    lmsr_buy_cost,
    lmsr_initial_subsidy,
    lmsr_price_yes,
    lmsr_sell_proceeds,
    shares_for_budget,
)
from models import WagerPick


def test_initial_price_is_fifty_fifty():
    assert lmsr_price_yes(0, 0, 100) == pytest.approx(0.5)


def test_buying_yes_raises_yes_price():
    before = lmsr_price_yes(0, 0, 100)
    cost = lmsr_buy_cost(0, 0, WagerPick.YES, 10, 100)
    after = lmsr_price_yes(10, 0, 100)
    assert cost > 0
    assert after > before


def test_buy_and_sell_round_trip_is_neutral_at_same_state():
    b = 100
    shares = 5.0
    cost = lmsr_buy_cost(0, 0, WagerPick.YES, shares, b)
    proceeds = lmsr_sell_proceeds(shares, 0, WagerPick.YES, shares, b)
    assert proceeds == pytest.approx(cost)


def test_shares_for_budget_respects_limit():
    shares, cost = shares_for_budget(0, 0, WagerPick.YES, 50, 100)
    assert shares > 0
    assert cost <= 50
    assert cost == math.ceil(lmsr_buy_cost(0, 0, WagerPick.YES, shares, 100))


def test_format_price_cents():
    assert format_price_cents(0.654) == "65¢"
    assert format_price_cents(0.004) == "0¢"
    assert format_price_cents(0.996) == "100¢"


def test_initial_subsidy_matches_formula():
    b = 100
    assert lmsr_initial_subsidy(b) == math.ceil(b * math.log(2))

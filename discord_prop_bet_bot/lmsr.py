"""Logarithmic Market Scoring Rule (LMSR) for prediction-market pricing."""

from __future__ import annotations

import math

from models import WagerPick


def lmsr_cost(q_yes: float, q_no: float, b: float) -> float:
    """Total LMSR cost for outstanding share quantities."""
    if b <= 0:
        raise ValueError("Liquidity parameter must be positive.")
    a = q_yes / b
    c = q_no / b
    m = max(a, c)
    return b * (m + math.log(math.exp(a - m) + math.exp(c - m)))


def lmsr_initial_subsidy(b: float) -> int:
    """Virtual coins seeded into escrow to cover maximum LMSR loss (b * ln 2)."""
    return int(math.ceil(b * math.log(2)))


def lmsr_price_yes(q_yes: float, q_no: float, b: float) -> float:
    """Implied probability of YES in [0, 1]."""
    ey = math.exp(q_yes / b)
    en = math.exp(q_no / b)
    return ey / (ey + en)


def lmsr_price_no(q_yes: float, q_no: float, b: float) -> float:
    return 1.0 - lmsr_price_yes(q_yes, q_no, b)


def lmsr_buy_cost(
    q_yes: float, q_no: float, side: WagerPick, shares: float, b: float
) -> float:
    """Coins required to buy `shares` of `side` at the current market state."""
    if shares <= 0:
        return 0.0
    if side == WagerPick.YES:
        return lmsr_cost(q_yes + shares, q_no, b) - lmsr_cost(q_yes, q_no, b)
    return lmsr_cost(q_yes, q_no + shares, b) - lmsr_cost(q_yes, q_no, b)


def lmsr_sell_proceeds(
    q_yes: float, q_no: float, side: WagerPick, shares: float, b: float
) -> float:
    """Coins returned when selling `shares` of `side` back to the market."""
    if shares <= 0:
        return 0.0
    if side == WagerPick.YES:
        if shares > q_yes + 1e-9:
            raise ValueError("Cannot sell more YES shares than the market holds.")
        return lmsr_cost(q_yes, q_no, b) - lmsr_cost(q_yes - shares, q_no, b)
    if shares > q_no + 1e-9:
        raise ValueError("Cannot sell more NO shares than the market holds.")
    return lmsr_cost(q_yes, q_no, b) - lmsr_cost(q_yes, q_no - shares, b)


def shares_for_budget(
    q_yes: float,
    q_no: float,
    side: WagerPick,
    budget: int,
    b: float,
    *,
    max_shares: float = 1_000_000.0,
) -> tuple[float, int]:
    """
    Maximum shares purchasable for at most `budget` coins.

    Returns (shares, coin_cost) where coin_cost = ceil(actual LMSR cost).
    """
    if budget <= 0:
        return 0.0, 0

    lo, hi = 0.0, 1.0
    while hi < max_shares and math.ceil(lmsr_buy_cost(q_yes, q_no, side, hi, b)) <= budget:
        lo = hi
        hi *= 2

    if hi >= max_shares and math.ceil(lmsr_buy_cost(q_yes, q_no, side, hi, b)) <= budget:
        shares = hi
    else:
        best = lo
        left, right = 0.0, hi
        for _ in range(64):
            mid = (left + right) / 2
            if math.ceil(lmsr_buy_cost(q_yes, q_no, side, mid, b)) <= budget:
                best = mid
                left = mid
            else:
                right = mid
        shares = best

    cost = int(math.ceil(lmsr_buy_cost(q_yes, q_no, side, shares, b)))
    return shares, cost


def format_price_cents(probability: float) -> str:
    """Format implied probability like Polymarket (e.g. 65¢)."""
    cents = int(round(probability * 100))
    cents = max(0, min(100, cents))
    return f"{cents}¢"

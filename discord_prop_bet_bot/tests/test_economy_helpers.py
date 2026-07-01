"""Tests for economy display helpers."""

from models import UserBalance

from bets import (
    build_leaderboard_description,
    format_anti_prestige,
    format_balance_message,
    format_leaderboard_wealth,
)


def test_user_balance_total_value():
    row = UserBalance(1, 10, 400, 0, portfolio_value=75)
    assert row.total_value == 475


def test_format_anti_prestige():
    assert format_anti_prestige(0) == ""
    assert format_anti_prestige(2) == " · ↩️×2"


def test_format_balance_message_with_debt_and_prestige():
    text = format_balance_message(UserBalance(1, 5, -200, reset_count=1))
    assert "bookie debt" in text
    assert "↩️×1" in text
    assert "/redeem" in text


def test_format_leaderboard_wealth_with_portfolio():
    row = UserBalance(1, 10, 500, 0, portfolio_value=120)
    assert format_leaderboard_wealth(row) == "**500** + **120** = **620** coins"


def test_format_leaderboard_wealth_cash_only():
    row = UserBalance(1, 10, 500, 0)
    assert format_leaderboard_wealth(row) == "**500** coins"


def test_build_leaderboard_description_mixed_tiers():
    rows = [
        UserBalance(1, 10, 500, 0),
        UserBalance(1, 20, 5000, 1),
    ]
    text = build_leaderboard_description(rows)
    assert "**No resets**" in text
    assert "**↩️×1+**" in text
    assert "↩️×1" in text


def test_build_leaderboard_description_clean_only():
    rows = [UserBalance(1, 10, 500, 0)]
    text = build_leaderboard_description(rows)
    assert "↩️" not in text
    assert "**No resets**" not in text

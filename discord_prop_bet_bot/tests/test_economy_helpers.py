"""Tests for economy display helpers."""

from models import UserBalance

from bets import build_leaderboard_description, format_anti_prestige, format_balance_message


def test_format_anti_prestige():
    assert format_anti_prestige(0) == ""
    assert format_anti_prestige(2) == " · ↩️×2"


def test_format_balance_message_with_debt_and_prestige():
    text = format_balance_message(UserBalance(1, 5, -200, reset_count=1))
    assert "bookie debt" in text
    assert "↩️×1" in text
    assert "/redeem" in text


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

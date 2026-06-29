"""Tests for duration parsing."""

import pytest

from bets import DurationParseError, parse_duration


def test_parse_hours():
    assert parse_duration("2h").total_seconds() == 7200
    assert parse_duration("1 hour").total_seconds() == 3600


def test_parse_minutes():
    assert parse_duration("30m").total_seconds() == 1800
    assert parse_duration("45 min").total_seconds() == 2700


def test_parse_days_and_seconds():
    assert parse_duration("1d").total_seconds() == 86400
    assert parse_duration("90s").total_seconds() == 90


def test_invalid_duration_raises():
    with pytest.raises(DurationParseError):
        parse_duration("not-a-duration")
    with pytest.raises(DurationParseError):
        parse_duration("0h")

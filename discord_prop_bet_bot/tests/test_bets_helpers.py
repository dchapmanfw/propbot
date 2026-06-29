"""Tests for bets.py helpers and embed builders."""

from __future__ import annotations

from datetime import datetime, timezone

from unittest.mock import MagicMock

import discord
import pytest

import bets as bets_module
import channel_policy as cp
from bets import (
    DurationParseError,
    build_bet_embed,
    build_help_embed,
    emoji_from_pick,
    parse_duration,
    pick_from_emoji,
    status_label,
)
from config import NO_EMOJI, YES_EMOJI
from models import Bet, BetOutcome, BetStatus, Wager, WagerPick


def _bet(**kwargs) -> Bet:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=1,
        guild_id=1,
        channel_id=100,
        message_id=999,
        creator_id=99,
        question="Will it rain?",
        close_time=now,
        yes_odds=2.0,
        no_odds=1.5,
        status=BetStatus.OPEN,
        outcome=None,
        created_at=now,
        escrow_balance=100,
        bookie_reserve=50,
    )
    defaults.update(kwargs)
    return Bet(**defaults)


def test_pick_from_emoji_and_emoji_from_pick():
    assert pick_from_emoji(YES_EMOJI) == WagerPick.YES
    assert pick_from_emoji(NO_EMOJI) == WagerPick.NO
    assert pick_from_emoji("🔥") is None
    assert emoji_from_pick(WagerPick.YES) == YES_EMOJI
    assert emoji_from_pick(WagerPick.NO) == NO_EMOJI


def test_status_label_all_statuses():
    for status in BetStatus:
        assert status.value.lower() in status_label(status).lower()


def test_parse_duration_seconds():
    assert parse_duration("45s").total_seconds() == 45


def test_build_bet_embed_open_without_wagers():
    embed = build_bet_embed(_bet(status=BetStatus.OPEN))
    data = embed.to_dict()
    assert "How to join" in str(data)


def test_build_bet_embed_open_with_wagers_and_creator():
    creator = MagicMock()
    creator.mention = "<@99>"
    wagers = [Wager(id=1, bet_id=1, user_id=50, pick=WagerPick.YES, amount=25)]
    embed = build_bet_embed(_bet(), creator=creator, wagers=wagers)
    assert "Participants" in str(embed.to_dict())


def test_build_bet_embed_truncates_many_wagers():
    wagers = [
        Wager(id=i, bet_id=1, user_id=1000 + i, pick=WagerPick.YES, amount=10)
        for i in range(16)
    ]
    embed = build_bet_embed(_bet(), wagers=wagers)
    assert "…and 1 more" in str(embed.to_dict())


def test_build_bet_embed_closed_shows_pool():
    embed = build_bet_embed(_bet(status=BetStatus.CLOSED))
    assert "Pool / reserve" in str(embed.to_dict())


def test_build_bet_embed_resolved_with_outcomes():
    for outcome in BetOutcome:
        embed = build_bet_embed(
            _bet(status=BetStatus.RESOLVED, outcome=outcome),
            footer_extra="Done",
        )
        assert "Outcome" in str(embed.to_dict())
        assert "Done" in embed.footer.text


def test_build_bet_embed_cancelled_without_creator():
    embed = build_bet_embed(_bet(status=BetStatus.CANCELLED, creator_id=42))
    assert "<@42>" in str(embed.to_dict())


def test_build_help_embed_with_allowed_channel(monkeypatch):
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", 12345)
    monkeypatch.setattr(bets_module, "ALLOWED_CHANNEL_ID", 12345)
    embed = build_help_embed()
    assert "<#12345>" in str(embed.to_dict())


def test_build_help_embed_without_allowed_channel(monkeypatch):
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", None)
    monkeypatch.setattr(bets_module, "ALLOWED_CHANNEL_ID", None)
    embed = build_help_embed()
    assert "any channel" in str(embed.to_dict()).lower()

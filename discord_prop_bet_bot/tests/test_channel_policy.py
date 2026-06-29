"""Tests for optional single-channel restriction."""

import channel_policy as cp
from channel_policy import allowed_channel_message, is_allowed_channel

def test_unset_allows_all_channels(monkeypatch):
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", None)
    assert is_allowed_channel(123)
    assert is_allowed_channel(None)


def test_set_restricts_to_matching_channel():
    assert is_allowed_channel(555, allowed_id=555)
    assert not is_allowed_channel(444, allowed_id=555)
    assert not is_allowed_channel(None, allowed_id=555)


def test_allowed_channel_message_mentions_channel():
    msg = allowed_channel_message(allowed_id=999888777)
    assert "<#999888777>" in msg


def test_allowed_channel_message_without_restriction(monkeypatch):
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", None)
    msg = allowed_channel_message()
    assert "not available" in msg.lower()

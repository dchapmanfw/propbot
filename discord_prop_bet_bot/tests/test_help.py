"""Tests for the /help embed."""

from bets import build_help_embed


def test_build_help_embed_has_core_sections():
    embed = build_help_embed()
    assert embed.title
    text = str(embed.to_dict())
    assert "bet_create" in text
    assert "bet_resolve" in text
    assert "bookie" in text.lower()
    assert "fictional" in text.lower()
    assert "no real-world value" in text.lower()
    assert "for fun" in text.lower()

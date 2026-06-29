"""Tests for PropBetBot channel helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot import PropBetBot


@pytest.fixture
def bot():
    with patch.object(PropBetBot, "__init__", lambda self: None):
        instance = PropBetBot()
        instance.get_channel = MagicMock(return_value=None)
        return instance


@pytest.mark.asyncio
async def test_fetch_channel_returns_cached_channel(bot):
    cached = MagicMock(spec=discord.TextChannel)
    bot.get_channel = MagicMock(return_value=cached)

    result = await bot.fetch_channel(123)

    assert result is cached


@pytest.mark.asyncio
async def test_fetch_channel_fetches_when_not_cached(bot):
    fetched = MagicMock(spec=discord.TextChannel)
    with patch.object(
        discord.Client, "fetch_channel", new_callable=AsyncMock, return_value=fetched
    ):
        result = await bot.fetch_channel(123)

    assert result is fetched


@pytest.mark.asyncio
async def test_fetch_channel_returns_none_on_http_error(bot):
    with patch.object(
        discord.Client,
        "fetch_channel",
        new_callable=AsyncMock,
        side_effect=discord.HTTPException(MagicMock(), "nope"),
    ):
        result = await bot.fetch_channel(123)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_channel_returns_none_for_non_messageable(bot):
    category = MagicMock(spec=discord.CategoryChannel)
    with patch.object(
        discord.Client, "fetch_channel", new_callable=AsyncMock, return_value=category
    ):
        result = await bot.fetch_channel(123)

    assert result is None

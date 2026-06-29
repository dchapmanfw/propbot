"""Tests for PropBetBot lifecycle, refresh, and background tasks."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bets import BetService, DurationParseError
from bot import PropBetBot, main
from database import Database
from models import BetStatus, WagerPick


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    await database.connect()
    yield database
    await database.close()
    os.unlink(path)


@pytest.fixture
def bot():
    return PropBetBot()


def test_prop_bet_bot_init_invalid_refund_duration():
    with patch("bot.parse_duration", side_effect=DurationParseError("bad")):
        with pytest.raises(SystemExit, match="UNRESOLVED_REFUND_AFTER"):
            PropBetBot()


def test_prop_bet_bot_init_success(bot):
    assert bot._unresolved_refund_after.total_seconds() > 0


@pytest.mark.asyncio
async def test_setup_hook_tracks_open_bets(bot, db):
    close = datetime.now(timezone.utc) + timedelta(hours=2)
    await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="?",
        close_time=close,
        yes_odds=2.0,
        no_odds=1.5,
    )
    bot.db = db
    bot.tree.sync = AsyncMock()
    bot.check_expired_bets.start = MagicMock()

    await bot.setup_hook()

    assert len(bot._open_bet_ids) == 1
    bot.check_expired_bets.start.assert_called_once()


@pytest.mark.asyncio
async def test_setup_hook_does_not_restart_running_task(bot, db):
    bot.db = db
    bot.tree.sync = AsyncMock()
    bot.check_expired_bets.is_running = MagicMock(return_value=True)
    bot.check_expired_bets.start = MagicMock()

    await bot.setup_hook()

    bot.check_expired_bets.start.assert_not_called()


@pytest.mark.asyncio
async def test_close_cancels_task_and_db(bot, db):
    bot.db = db
    bot.check_expired_bets.cancel = MagicMock()
    with patch.object(discord.ext.commands.Bot, "close", new_callable=AsyncMock):
        await bot.close()
    bot.check_expired_bets.cancel.assert_called_once()


def test_track_and_untrack_bet(bot):
    bet = MagicMock()
    bet.id = 7
    bot.track_open_bet(bet)
    assert 7 in bot._open_bet_ids
    bot.untrack_bet(7)
    assert 7 not in bot._open_bet_ids


@pytest.mark.asyncio
async def test_refresh_bet_message_noops(bot, db):
    bot.db = db
    await bot.refresh_bet_message(9999)

    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="?",
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
        yes_odds=2.0,
        no_odds=1.5,
    )
    await bot.refresh_bet_message(bet.id)

    await db.set_bet_message_id(bet.id, 4242)
    bot.fetch_channel = AsyncMock(return_value=None)
    await bot.refresh_bet_message(bet.id)


@pytest.mark.asyncio
async def test_refresh_bet_message_edits_embed(bot, db):
    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="?",
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
        yes_odds=2.0,
        no_odds=1.5,
    )
    await db.set_bet_message_id(bet.id, 4242)
    bot.db = db

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    channel.guild = MagicMock()
    channel.guild.get_member = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(return_value=channel)

    await bot.refresh_bet_message(bet.id, footer_extra="Updated")
    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_market_message_edits_embed(bot, db):
    from markets import MarketService

    service = MarketService(db)
    bet = await service.create_market(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Rain?",
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    await db.set_bet_message_id(bet.id, 4242)
    bot.db = db

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    channel.guild = MagicMock()
    channel.guild.get_member = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(return_value=channel)

    await bot.refresh_bet_message(bet.id)
    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_bet_message_handles_not_found(bot, db):
    bet = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="?",
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
        yes_odds=2.0,
        no_odds=1.5,
    )
    await db.set_bet_message_id(bet.id, 4242)
    bot.db = db

    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), ""))
    bot.fetch_channel = AsyncMock(return_value=channel)

    await bot.refresh_bet_message(bet.id)


@pytest.mark.asyncio
async def test_check_expired_bets_closes_and_refunds(bot, db):
    bot.db = db
    svc = BetService(db)
    recent_past = datetime.now(timezone.utc) - timedelta(minutes=5)
    stale_past = datetime.now(timezone.utc) - timedelta(hours=48)

    expired = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Expired?",
        close_time=recent_past,
        yes_odds=2.0,
        no_odds=1.5,
    )

    stale = await db.create_bet(
        guild_id=1,
        channel_id=100,
        creator_id=99,
        question="Stale closed?",
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
        yes_odds=2.0,
        no_odds=1.5,
    )
    await db.ensure_user(1, 50)
    await svc.place_or_update_wager(1, stale.id, 50, WagerPick.YES, 50)
    await svc.close_bet(stale.id)
    await db.conn.execute(
        "UPDATE bets SET close_time = ? WHERE id = ?",
        (stale_past.isoformat(), stale.id),
    )
    await db.conn.commit()

    bot.refresh_bet_message = AsyncMock()
    bot.untrack_bet = MagicMock()

    await bot.check_expired_bets()

    assert (await db.get_bet(expired.id)).status == BetStatus.CLOSED
    assert (await db.get_bet(stale.id)).status == BetStatus.CANCELLED
    assert bot.refresh_bet_message.await_count >= 2


@pytest.mark.asyncio
async def test_before_check_expired_bets_waits(bot):
    bot.wait_until_ready = AsyncMock()
    await bot.before_check_expired_bets()
    bot.wait_until_ready.assert_awaited_once()


def test_main_exits_without_token():
    with patch("bot.DISCORD_TOKEN", None):
        with pytest.raises(SystemExit):
            main()


def test_main_runs_bot():
    mock_bot = MagicMock()
    with patch("bot.DISCORD_TOKEN", "token"), patch("bot.PropBetBot", return_value=mock_bot):
        main()
    mock_bot.run.assert_called_once_with("token")

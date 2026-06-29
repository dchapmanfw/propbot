"""Tests for slash commands, modals, and views."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord import app_commands

import channel_policy as cp
from bets import BetService
from commands import PropBetCommands, WagerButtonView, WagerModal
from config import NO_EMOJI, YES_EMOJI
from models import BetOutcome, BetStatus, WagerPick
from tests.conftest import call_slash, make_interaction

BOOKIE_ID = 99
BETTOR_ID = 50
CHANNEL_ID = 100


async def _open_bet(db, *, creator_id=BOOKIE_ID, hours=2, message_id=4242):
    close = datetime.now(timezone.utc) + timedelta(hours=hours)
    bet = await db.create_bet(
        guild_id=1,
        channel_id=CHANNEL_ID,
        creator_id=creator_id,
        question="Test bet?",
        close_time=close,
        yes_odds=2.0,
        no_odds=1.5,
    )
    if message_id is not None:
        await db.set_bet_message_id(bet.id, message_id)
    return bet.id


# --- helpers / admin ---


@pytest.mark.asyncio
async def test_require_allowed_channel_rejects(cog, monkeypatch):
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", 999)
    interaction = make_interaction(channel_id=CHANNEL_ID)
    allowed = await cog._require_allowed_channel(interaction)
    assert allowed is False
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_is_admin_or_creator(cog):
    interaction = make_interaction(user_id=BETTOR_ID, admin=False)
    assert await cog._is_admin_or_creator(interaction, BOOKIE_ID) is False
    assert await cog._is_admin_or_creator(interaction, BETTOR_ID) is True
    interaction = make_interaction(user_id=BETTOR_ID, admin=True)
    assert await cog._is_admin_or_creator(interaction, BOOKIE_ID) is True
    interaction = make_interaction(guild=False)
    assert await cog._is_admin_or_creator(interaction, BOOKIE_ID) is False


@pytest.mark.asyncio
async def test_notify_prefer_channel_http_error(cog, bot_mock):
    user = MagicMock()
    user.id = BETTOR_ID
    user.mention = "<@50>"
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "fail"))
    bot_mock.fetch_channel.return_value = channel
    await cog._notify_user(user, CHANNEL_ID, "hi", prefer_channel=True)


@pytest.mark.asyncio
async def test_notify_channel_fallback_http_error(cog, bot_mock):
    user = MagicMock()
    user.id = BETTOR_ID
    user.mention = "<@50>"
    user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "dm"))
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "fail"))
    bot_mock.fetch_channel.return_value = channel
    await cog._notify_user(user, CHANNEL_ID, "hi")


# --- WagerModal / WagerButtonView ---


@pytest.mark.asyncio
async def test_wager_modal_invalid_amount(bot_mock):
    modal = WagerModal(bot_mock, 1, WagerPick.YES, 1)
    modal.amount_input = MagicMock()
    modal.amount_input.value = "abc"
    interaction = make_interaction()
    await modal.on_submit(interaction)
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_wager_modal_success(bot_mock, db):
    bot_mock.db = db
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_ID)
    modal = WagerModal(bot_mock, bet_id, WagerPick.YES, 1)
    modal.amount_input = MagicMock()
    modal.amount_input.value = "50"
    interaction = make_interaction(user_id=BETTOR_ID)
    await modal.on_submit(interaction)
    interaction.response.send_message.assert_awaited_once()
    bot_mock.refresh_bet_message.assert_awaited_once_with(bet_id)


@pytest.mark.asyncio
async def test_wager_modal_service_error(bot_mock, db):
    bot_mock.db = db
    modal = WagerModal(bot_mock, 9999, WagerPick.YES, 1)
    modal.amount_input = MagicMock()
    modal.amount_input.value = "50"
    interaction = make_interaction()
    await modal.on_submit(interaction)
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_wager_button_view_wrong_user(bot_mock):
    view = WagerButtonView(bot_mock, 1, WagerPick.YES, 1, owner_id=BOOKIE_ID)
    interaction = make_interaction(user_id=BETTOR_ID)
    assert await view.interaction_check(interaction) is False


@pytest.mark.asyncio
async def test_wager_button_view_opens_modal(bot_mock):
    view = WagerButtonView(bot_mock, 1, WagerPick.YES, 1, owner_id=BETTOR_ID)
    interaction = make_interaction(user_id=BETTOR_ID)
    assert await view.interaction_check(interaction) is True
    button = view.children[0]
    await button.callback(interaction)
    interaction.response.send_modal.assert_awaited_once()


# --- slash commands ---


@pytest.mark.asyncio
async def test_help_command_no_guild(cog):
    interaction = make_interaction(guild=False)
    await call_slash(cog, cog.help_command, interaction)
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_help_command_wrong_channel_hint(cog, monkeypatch):
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", 999)
    interaction = make_interaction(channel_id=CHANNEL_ID)
    await call_slash(cog, cog.help_command, interaction)
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_balance_command(cog, db):
    interaction = make_interaction(user_id=BETTOR_ID)
    await call_slash(cog, cog.balance, interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_balance_command_no_guild(cog):
    interaction = make_interaction(guild=False)
    await call_slash(cog, cog.balance, interaction)
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_bet_create_validation_paths(cog, db):
    interaction = make_interaction(guild=False)
    await call_slash(cog, cog.bet_create, interaction, "Q?", "2h", 1.5, 2.0)
    interaction.response.send_message.assert_awaited_once()

    interaction = make_interaction()
    await call_slash(cog, cog.bet_create, interaction, "Q?", "2h", -1.0, 2.0)
    interaction.response.send_message.assert_awaited_once()

    interaction = make_interaction()
    await call_slash(cog, cog.bet_create, interaction, "Q?", "bad", 1.5, 2.0)
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_bet_create_success(cog, db, bot_mock):
    interaction = make_interaction(user_id=BOOKIE_ID)
    message = MagicMock()
    message.id = 8888
    message.add_reaction = AsyncMock()
    interaction.original_response = AsyncMock(return_value=message)

    await call_slash(cog, cog.bet_create, interaction, "Will it work?", "2h", 1.5, 2.0)

    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()
    bot_mock.track_open_bet.assert_called_once()


@pytest.mark.asyncio
async def test_bet_create_reaction_forbidden(cog, db, bot_mock):
    interaction = make_interaction(user_id=BOOKIE_ID)
    message = MagicMock()
    message.id = 8888
    message.add_reaction = AsyncMock(side_effect=discord.Forbidden(MagicMock(), ""))
    message.delete = AsyncMock()
    interaction.original_response = AsyncMock(return_value=message)

    await call_slash(cog, cog.bet_create, interaction, "Will it work?", "2h", 1.5, 2.0)

    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_bet_resolve_paths(cog, db, bot_mock):
    interaction = make_interaction(guild=False)
    await call_slash(
        cog, cog.bet_resolve, interaction, 1, app_commands.Choice(name="YES", value="yes")
    )
    interaction.response.send_message.assert_awaited_once()

    interaction = make_interaction()
    await call_slash(
        cog, cog.bet_resolve, interaction, 9999, app_commands.Choice(name="YES", value="yes")
    )
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()

    bet_id = await _open_bet(db)
    interaction = make_interaction(user_id=BETTOR_ID)
    await call_slash(
        cog, cog.bet_resolve, interaction, bet_id, app_commands.Choice(name="YES", value="yes")
    )
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()

    interaction = make_interaction(user_id=BOOKIE_ID)
    await call_slash(
        cog, cog.bet_resolve, interaction, bet_id, app_commands.Choice(name="YES", value="yes")
    )
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_bet_resolve_success_with_refund(cog, db, bot_mock):
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_ID)
    svc = BetService(db)
    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 50)
    await svc.close_bet(bet_id)

    interaction = make_interaction(user_id=BOOKIE_ID)
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    bot_mock.get_channel = MagicMock(return_value=channel)

    await call_slash(
        cog,
        cog.bet_resolve,
        interaction,
        bet_id,
        app_commands.Choice(name="Refund", value="refund"),
    )
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    bot_mock.untrack_bet.assert_called_once_with(bet_id)


@pytest.mark.asyncio
async def test_bet_resolve_winner_message(cog, db, bot_mock):
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_ID)
    svc = BetService(db)
    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 50)
    await svc.close_bet(bet_id)

    interaction = make_interaction(user_id=BOOKIE_ID)
    bot_mock.get_channel = MagicMock(return_value=None)

    await call_slash(
        cog,
        cog.bet_resolve,
        interaction,
        bet_id,
        app_commands.Choice(name="YES", value="yes"),
    )
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_bet_resolve_auto_closes_expired_open_bet(cog, db):
    bet_id = await _open_bet(db, hours=-1)
    interaction = make_interaction(user_id=BOOKIE_ID)
    await call_slash(
        cog, cog.bet_resolve, interaction, bet_id, app_commands.Choice(name="YES", value="yes")
    )
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_bet_cancel_paths(cog, db, bot_mock):
    interaction = make_interaction(guild=False)
    await call_slash(cog, cog.bet_cancel, interaction, 1)
    interaction.response.send_message.assert_awaited_once()

    interaction = make_interaction()
    await call_slash(cog, cog.bet_cancel, interaction, 9999)
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()

    bet_id = await _open_bet(db)
    interaction = make_interaction(user_id=BETTOR_ID)
    await call_slash(cog, cog.bet_cancel, interaction, bet_id)
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()

    interaction = make_interaction(user_id=BOOKIE_ID)
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    bot_mock.get_channel = MagicMock(return_value=channel)
    await call_slash(cog, cog.bet_cancel, interaction, bet_id)
    bot_mock.untrack_bet.assert_called_once_with(bet_id)


@pytest.mark.asyncio
async def test_bet_cancel_already_resolved(cog, db):
    bet_id = await _open_bet(db)
    await db.update_bet_status(bet_id, BetStatus.RESOLVED, BetOutcome.YES)
    interaction = make_interaction(user_id=BOOKIE_ID)
    await call_slash(cog, cog.bet_cancel, interaction, bet_id)
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_bet_status_paths(cog, db):
    interaction = make_interaction(guild=False)
    await call_slash(cog, cog.bet_status, interaction, 1)

    interaction = make_interaction()
    await call_slash(cog, cog.bet_status, interaction, 9999)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()

    bet_id = await _open_bet(db)
    interaction = make_interaction()
    await call_slash(cog, cog.bet_status, interaction, bet_id)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_my_bets_paths(cog, db):
    interaction = make_interaction(guild=False)
    await call_slash(cog, cog.my_bets, interaction)

    interaction = make_interaction(user_id=777)
    await call_slash(cog, cog.my_bets, interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()

    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_ID)
    await db.upsert_wager(bet_id, BETTOR_ID, WagerPick.YES, 10)
    interaction = make_interaction(user_id=BETTOR_ID)
    await call_slash(cog, cog.my_bets, interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_leaderboard_paths(cog, db):
    interaction = make_interaction(guild=False)
    await call_slash(cog, cog.leaderboard, interaction)

    interaction = make_interaction()
    await call_slash(cog, cog.leaderboard, interaction)
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()

    await db.ensure_user(1, BETTOR_ID)
    interaction = make_interaction()
    await call_slash(cog, cog.leaderboard, interaction)
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_bet_message(cog, db, bot_mock):
    bet = await db.get_bet(await _open_bet(db))
    assert bet is not None
    bot_mock.get_channel = MagicMock(return_value=None)
    assert await cog._get_bet_message(bet) is None

    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), ""))
    bot_mock.get_channel = MagicMock(return_value=channel)
    assert await cog._get_bet_message(bet) is None


@pytest.mark.asyncio
async def test_reconcile_handles_missing_message_and_forbidden(cog, db, bot_mock):
    bet_id = await _open_bet(db)
    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), ""))
    bot_mock.fetch_channel.return_value = channel
    await cog._reconcile_open_bet_reactions()

    channel.fetch_message = AsyncMock(side_effect=discord.Forbidden(MagicMock(), ""))
    await cog._reconcile_open_bet_reactions()


@pytest.mark.asyncio
async def test_reconcile_skips_when_channel_unavailable(cog, db, bot_mock):
    await _open_bet(db)
    bot_mock.fetch_channel.return_value = None
    await cog._reconcile_open_bet_reactions()


@pytest.mark.asyncio
async def test_on_raw_reaction_remove_refunds(cog, db, bot_mock):
    bet_id = await _open_bet(db)
    await db.ensure_user(1, BETTOR_ID)
    svc = BetService(db)
    await svc.place_or_update_wager(1, bet_id, BETTOR_ID, WagerPick.YES, 40)

    payload = SimpleNamespace(
        user_id=BETTOR_ID,
        channel_id=CHANNEL_ID,
        message_id=4242,
        emoji=YES_EMOJI,
    )
    cog._notify_user = AsyncMock()
    await cog.on_raw_reaction_remove(payload)
    bot_mock.refresh_bet_message.assert_awaited_once_with(bet_id)


@pytest.mark.asyncio
async def test_on_raw_reaction_remove_ignores_non_matching(cog, db):
    bet_id = await _open_bet(db)
    payload = SimpleNamespace(
        user_id=BETTOR_ID,
        channel_id=CHANNEL_ID,
        message_id=4242,
        emoji=NO_EMOJI,
    )
    await cog.on_raw_reaction_remove(payload)

    payload = SimpleNamespace(
        user_id=1,
        channel_id=CHANNEL_ID,
        message_id=4242,
        emoji=YES_EMOJI,
    )
    await cog.on_raw_reaction_remove(payload)

    payload = SimpleNamespace(
        user_id=BETTOR_ID,
        channel_id=999,
        message_id=4242,
        emoji=YES_EMOJI,
    )
    await cog.on_raw_reaction_remove(payload)

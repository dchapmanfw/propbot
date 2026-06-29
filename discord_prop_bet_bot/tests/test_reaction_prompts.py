"""Tests for reaction wager prompts and startup reconciliation."""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import channel_policy as cp
from bets import BetService
from commands import PropBetCommands, WAGER_PROMPT_COOLDOWN
from config import NO_EMOJI, YES_EMOJI
from database import Database
from models import BetStatus, WagerPick

BOOKIE_ID = 99
BETTOR_ID = 50
MESSAGE_ID = 4242
CHANNEL_ID = 100
GUILD_ID = 1


@pytest.fixture
async def reaction_cog(monkeypatch):
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", None)

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.connect()

    bot = MagicMock()
    bot.db = db
    bot.user = MagicMock()
    bot.user.id = 1
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(return_value=MagicMock())
    bot.fetch_channel = AsyncMock()
    bot.refresh_bet_message = AsyncMock()
    bot._wager_prompt_at = {}
    cog = PropBetCommands(bot)

    yield cog, db, bot
    await db.close()
    os.unlink(path)


async def _open_bet(
    db: Database,
    *,
    message_id: int | None = MESSAGE_ID,
    creator_id: int = BOOKIE_ID,
    hours_until_close: float = 2,
) -> int:
    close = datetime.now(timezone.utc) + timedelta(hours=hours_until_close)
    bet = await db.create_bet(
        guild_id=GUILD_ID,
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


async def _get_bet(db: Database, bet_id: int = 1):
    bet = await db.get_bet(bet_id)
    assert bet is not None
    return bet


def _reaction_payload(
    *,
    user_id: int = BETTOR_ID,
    emoji: str = YES_EMOJI,
    message_id: int = MESSAGE_ID,
    channel_id: int = CHANNEL_ID,
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        emoji=emoji,
        message_id=message_id,
        channel_id=channel_id,
    )


async def _user_iter(users):
    for user in users:
        yield user


# --- _notify_user ---


@pytest.mark.asyncio
async def test_notify_user_sends_dm_when_available(reaction_cog):
    cog, _db, bot = reaction_cog
    user = MagicMock()
    user.send = AsyncMock()
    channel = MagicMock()
    bot.fetch_channel.return_value = channel

    await cog._notify_user(user, CHANNEL_ID, "hello")

    user.send.assert_awaited_once_with("hello", view=None)
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_notify_user_prefer_channel_skips_dm(reaction_cog):
    cog, _db, bot = reaction_cog
    user = MagicMock()
    user.id = BETTOR_ID
    user.mention = f"<@{BETTOR_ID}>"
    user.send = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.fetch_channel.return_value = channel

    await cog._notify_user(
        user, CHANNEL_ID, "cannot wager", prefer_channel=True, delete_after=15
    )

    user.send.assert_not_awaited()
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_user_fetches_channel_when_dm_blocked(reaction_cog):
    cog, _db, bot = reaction_cog
    user = MagicMock()
    user.id = BETTOR_ID
    user.mention = f"<@{BETTOR_ID}>"
    user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "dm blocked"))

    channel = MagicMock()
    channel.send = AsyncMock()
    bot.fetch_channel.return_value = channel

    await cog._notify_user(user, CHANNEL_ID, "hello")

    bot.fetch_channel.assert_awaited_once_with(CHANNEL_ID)
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_user_does_nothing_when_dm_and_channel_unavailable(reaction_cog):
    cog, _db, bot = reaction_cog
    user = MagicMock()
    user.id = BETTOR_ID
    user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "dm blocked"))
    bot.fetch_channel.return_value = None

    await cog._notify_user(user, CHANNEL_ID, "hello")

    user.send.assert_awaited_once()


# --- _should_prompt_wager ---


def test_should_prompt_wager_respects_cooldown(reaction_cog):
    cog, _db, bot = reaction_cog
    bot._wager_prompt_at[(BETTOR_ID, 1)] = time.monotonic()

    assert cog._should_prompt_wager(BETTOR_ID, 1) is False


def test_should_prompt_wager_allows_after_cooldown(reaction_cog):
    cog, _db, bot = reaction_cog
    bot._wager_prompt_at[(BETTOR_ID, 1)] = (
        time.monotonic() - WAGER_PROMPT_COOLDOWN - 1
    )

    assert cog._should_prompt_wager(BETTOR_ID, 1) is True


# --- _maybe_prompt_wager_for_reaction ---


@pytest.mark.asyncio
async def test_maybe_prompt_skips_bot_user(reaction_cog):
    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db)
    bet = await _get_bet(db, bet_id)
    cog._notify_user = AsyncMock()

    await cog._maybe_prompt_wager_for_reaction(
        bet, bot.user.id, WagerPick.YES, CHANNEL_ID
    )

    cog._notify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_prompt_notifies_creator_in_channel(reaction_cog):
    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db)
    bet = await _get_bet(db, bet_id)
    cog._notify_user = AsyncMock()

    await cog._maybe_prompt_wager_for_reaction(
        bet, BOOKIE_ID, WagerPick.YES, CHANNEL_ID
    )

    cog._notify_user.assert_awaited_once()
    assert cog._notify_user.await_args.kwargs["prefer_channel"] is True


@pytest.mark.asyncio
async def test_maybe_prompt_skips_matching_existing_wager(reaction_cog):
    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db)
    await db.ensure_user(GUILD_ID, BETTOR_ID)
    await db.upsert_wager(bet_id, BETTOR_ID, WagerPick.YES, 25)
    bet = await _get_bet(db, bet_id)
    cog._notify_user = AsyncMock()

    await cog._maybe_prompt_wager_for_reaction(
        bet, BETTOR_ID, WagerPick.YES, CHANNEL_ID
    )

    cog._notify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_prompt_prompts_when_pick_differs_from_existing_wager(reaction_cog):
    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db)
    await db.ensure_user(GUILD_ID, BETTOR_ID)
    await db.upsert_wager(bet_id, BETTOR_ID, WagerPick.NO, 25)
    bet = await _get_bet(db, bet_id)
    cog._notify_user = AsyncMock()

    await cog._maybe_prompt_wager_for_reaction(
        bet, BETTOR_ID, WagerPick.YES, CHANNEL_ID
    )

    cog._notify_user.assert_awaited_once()
    prompt = cog._notify_user.await_args.args[2]
    assert "Max wager:" in prompt


@pytest.mark.asyncio
async def test_maybe_prompt_max_wager_includes_existing_escrow(reaction_cog):
    from config import STARTING_BALANCE

    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db)
    await db.ensure_user(GUILD_ID, BETTOR_ID)
    await db.adjust_balance(GUILD_ID, BETTOR_ID, -800)
    await db.upsert_wager(bet_id, BETTOR_ID, WagerPick.NO, 100)
    bet = await _get_bet(db, bet_id)
    cog._notify_user = AsyncMock()

    await cog._maybe_prompt_wager_for_reaction(
        bet, BETTOR_ID, WagerPick.YES, CHANNEL_ID
    )

    expected_max = STARTING_BALANCE - 800 + 100
    prompt = cog._notify_user.await_args.args[2]
    assert f"**{expected_max}**" in prompt


@pytest.mark.asyncio
async def test_maybe_prompt_closes_expired_bet(reaction_cog):
    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db, hours_until_close=-1)
    bet = await _get_bet(db, bet_id)
    cog._notify_user = AsyncMock()

    await cog._maybe_prompt_wager_for_reaction(
        bet, BETTOR_ID, WagerPick.YES, CHANNEL_ID
    )

    cog._notify_user.assert_not_awaited()
    bot.refresh_bet_message.assert_awaited_once_with(bet_id)
    updated = await db.get_bet(bet_id)
    assert updated is not None
    assert updated.status == BetStatus.CLOSED


@pytest.mark.asyncio
async def test_maybe_prompt_respects_cooldown(reaction_cog):
    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db)
    bet = await _get_bet(db, bet_id)
    bot._wager_prompt_at[(BETTOR_ID, bet_id)] = time.monotonic()
    cog._notify_user = AsyncMock()

    await cog._maybe_prompt_wager_for_reaction(
        bet, BETTOR_ID, WagerPick.YES, CHANNEL_ID
    )

    cog._notify_user.assert_not_awaited()


# --- on_raw_reaction_add ---


@pytest.mark.asyncio
async def test_on_raw_reaction_add_ignores_bot_reactions(reaction_cog):
    cog, db, bot = reaction_cog
    await _open_bet(db)
    cog._maybe_prompt_wager_for_reaction = AsyncMock()

    await cog.on_raw_reaction_add(_reaction_payload(user_id=bot.user.id))

    cog._maybe_prompt_wager_for_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_raw_reaction_add_ignores_wrong_emoji(reaction_cog):
    cog, db, bot = reaction_cog
    await _open_bet(db)
    cog._maybe_prompt_wager_for_reaction = AsyncMock()

    await cog.on_raw_reaction_add(_reaction_payload(emoji="🔥"))

    cog._maybe_prompt_wager_for_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_raw_reaction_add_ignores_unknown_message(reaction_cog):
    cog, db, bot = reaction_cog
    await _open_bet(db)
    cog._maybe_prompt_wager_for_reaction = AsyncMock()

    await cog.on_raw_reaction_add(_reaction_payload(message_id=999999))

    cog._maybe_prompt_wager_for_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_raw_reaction_add_ignores_closed_bets(reaction_cog):
    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db)
    await BetService(db).close_bet(bet_id)
    cog._maybe_prompt_wager_for_reaction = AsyncMock()

    await cog.on_raw_reaction_add(_reaction_payload())

    cog._maybe_prompt_wager_for_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_raw_reaction_add_ignores_disallowed_channel(
    reaction_cog, monkeypatch
):
    cog, db, bot = reaction_cog
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", 999)
    await _open_bet(db)
    cog._maybe_prompt_wager_for_reaction = AsyncMock()

    await cog.on_raw_reaction_add(_reaction_payload(channel_id=CHANNEL_ID))

    cog._maybe_prompt_wager_for_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_raw_reaction_add_prompts_bettor(reaction_cog):
    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db)
    bet = await _get_bet(db, bet_id)
    cog._maybe_prompt_wager_for_reaction = AsyncMock()

    await cog.on_raw_reaction_add(_reaction_payload())

    cog._maybe_prompt_wager_for_reaction.assert_awaited_once_with(
        bet,
        BETTOR_ID,
        WagerPick.YES,
        CHANNEL_ID,
    )


@pytest.mark.asyncio
async def test_on_raw_reaction_add_handles_no_pick(reaction_cog):
    cog, db, bot = reaction_cog
    await _open_bet(db)
    cog._maybe_prompt_wager_for_reaction = AsyncMock()

    await cog.on_raw_reaction_add(_reaction_payload(emoji=NO_EMOJI))

    cog._maybe_prompt_wager_for_reaction.assert_awaited_once()
    assert cog._maybe_prompt_wager_for_reaction.await_args.args[2] == WagerPick.NO


# --- _reconcile_open_bet_reactions ---


@pytest.mark.asyncio
async def test_reconcile_prompts_users_with_reactions_but_no_wager(reaction_cog):
    cog, db, bot = reaction_cog
    await _open_bet(db)

    reactor = MagicMock()
    reactor.bot = False
    reactor.id = BETTOR_ID

    reaction = MagicMock()
    reaction.emoji = YES_EMOJI
    reaction.users = MagicMock(return_value=_user_iter([reactor]))

    message = MagicMock()
    message.reactions = [reaction]

    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot.fetch_channel.return_value = channel
    bot.fetch_user = AsyncMock(return_value=MagicMock())

    cog._notify_user = AsyncMock()

    await cog._reconcile_open_bet_reactions()

    cog._notify_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_skips_users_with_matching_wager(reaction_cog):
    cog, db, bot = reaction_cog
    bet_id = await _open_bet(db)
    await db.ensure_user(GUILD_ID, BETTOR_ID)
    await db.upsert_wager(bet_id, BETTOR_ID, WagerPick.YES, 25)

    reactor = MagicMock()
    reactor.bot = False
    reactor.id = BETTOR_ID

    reaction = MagicMock()
    reaction.emoji = YES_EMOJI
    reaction.users = MagicMock(return_value=_user_iter([reactor]))

    message = MagicMock()
    message.reactions = [reaction]

    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot.fetch_channel.return_value = channel

    cog._notify_user = AsyncMock()

    await cog._reconcile_open_bet_reactions()

    cog._notify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_skips_open_bets_without_message_id(reaction_cog):
    cog, db, bot = reaction_cog
    await _open_bet(db, message_id=None)
    bot.fetch_channel = AsyncMock()

    await cog._reconcile_open_bet_reactions()

    bot.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_skips_creator_reactions(reaction_cog):
    cog, db, bot = reaction_cog
    await _open_bet(db)

    creator = MagicMock()
    creator.bot = False
    creator.id = BOOKIE_ID

    reaction = MagicMock()
    reaction.emoji = YES_EMOJI
    reaction.users = MagicMock(return_value=_user_iter([creator]))

    message = MagicMock()
    message.reactions = [reaction]

    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot.fetch_channel.return_value = channel

    cog._maybe_prompt_wager_for_reaction = AsyncMock()

    await cog._reconcile_open_bet_reactions()

    cog._maybe_prompt_wager_for_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_skips_non_wager_emojis(reaction_cog):
    cog, db, bot = reaction_cog
    await _open_bet(db)

    reactor = MagicMock()
    reactor.bot = False
    reactor.id = BETTOR_ID

    reaction = MagicMock()
    reaction.emoji = "🔥"
    reaction.users = MagicMock(return_value=_user_iter([reactor]))

    message = MagicMock()
    message.reactions = [reaction]

    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot.fetch_channel.return_value = channel

    cog._maybe_prompt_wager_for_reaction = AsyncMock()

    await cog._reconcile_open_bet_reactions()

    cog._maybe_prompt_wager_for_reaction.assert_not_awaited()


# --- on_ready ---


@pytest.mark.asyncio
async def test_on_ready_reconciles_only_once(reaction_cog):
    cog, _db, _bot = reaction_cog
    cog._reconcile_open_bet_reactions = AsyncMock()

    await cog.on_ready()
    await cog.on_ready()

    cog._reconcile_open_bet_reactions.assert_awaited_once()

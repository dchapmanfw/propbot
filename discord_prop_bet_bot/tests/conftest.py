"""Shared fixtures for command and bot tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import channel_policy as cp
from commands import PropBetCommands
from database import Database

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Tiered coverage policy (enforced in pytest_terminal_summary when --cov is active).
TOTAL_COVERAGE_MIN = 75
MODULE_COVERAGE_MIN = {
    "bets.py": 90,
    "database.py": 90,
    "models.py": 100,
    "channel_policy.py": 90,
    "config.py": 80,
    "commands.py": 70,
    "bot.py": 70,
}


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
def bot_mock(db):
    bot = MagicMock()
    bot.db = db
    bot.user = MagicMock()
    bot.user.id = 1
    bot.get_user = MagicMock(return_value=None)
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(return_value=MagicMock())
    bot.fetch_channel = AsyncMock()
    bot.refresh_bet_message = AsyncMock()
    bot.track_open_bet = MagicMock()
    bot.untrack_bet = MagicMock()
    bot._wager_prompt_at = {}
    return bot


@pytest.fixture
async def cog(bot_mock, monkeypatch):
    monkeypatch.setattr(cp, "ALLOWED_CHANNEL_ID", None)
    return PropBetCommands(bot_mock)


def make_interaction(
    *,
    guild: bool = True,
    channel_id: int = 100,
    user_id: int = 50,
    admin: bool = False,
):
    interaction = AsyncMock()
    interaction.response = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.channel_id = channel_id

    if guild:
        interaction.guild = MagicMock()
        interaction.guild.id = 1
        interaction.guild.name = "Test Server"
        interaction.guild.get_member = MagicMock(return_value=None)
        interaction.user.guild_permissions = MagicMock()
        interaction.user.guild_permissions.administrator = admin
        interaction.user.guild_permissions.manage_guild = admin
        interaction.channel = MagicMock()
        interaction.channel.id = channel_id
        interaction.channel.mention = f"<#{channel_id}>"
    else:
        interaction.guild = None
        interaction.channel = None

    return interaction


async def call_slash(cog, command, interaction, /, *args, **kwargs):
    """Invoke a discord.py app command callback."""
    await command.callback(cog, interaction, *args, **kwargs)


def _module_coverage_percent(cov, rel_path: str) -> float:
    abs_path = str(PROJECT_ROOT / rel_path)
    if abs_path not in cov.get_data().measured_files():
        return 100.0
    analysis = cov.analysis2(abs_path)
    statements = analysis[1]
    missing = analysis[3]
    if not statements:
        return 100.0
    return 100.0 * (len(statements) - len(missing)) / len(statements)


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Enforce tiered per-module and total coverage when pytest-cov is enabled."""
    if not getattr(config.option, "cov_source", None):
        return

    cov_plugin = config.pluginmanager.getplugin("_cov")
    if cov_plugin is None or cov_plugin.cov_controller.cov is None:
        return

    cov = cov_plugin.cov_controller.cov
    failures: list[str] = []

    total = cov.report(file=open(os.devnull, "w"), precision=1)
    if total < TOTAL_COVERAGE_MIN:
        failures.append(
            f"Total coverage {total:.1f}% is below required {TOTAL_COVERAGE_MIN}%"
        )

    for rel_path, minimum in sorted(MODULE_COVERAGE_MIN.items()):
        percent = _module_coverage_percent(cov, rel_path)
        if percent + 0.05 < minimum:  # small float tolerance
            failures.append(
                f"{rel_path}: {percent:.1f}% covered (minimum {minimum}%)"
            )

    if failures:
        terminalreporter.write_sep("=", "Coverage policy failures")
        for message in failures:
            terminalreporter.write_line(message, red=True)
        terminalreporter._session.exitstatus = 1

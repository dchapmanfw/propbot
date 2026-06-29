"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (parent of this package directory).
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)

DISCORD_TOKEN: str | None = os.getenv("DISCORD_TOKEN")
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "propbot.db")
STARTING_BALANCE: int = int(os.getenv("STARTING_BALANCE", "1000"))

# How often the background task checks for expired bets (seconds).
BET_EXPIRY_CHECK_INTERVAL: int = int(os.getenv("BET_EXPIRY_CHECK_INTERVAL", "30"))

# Emoji used for YES / NO reactions on bet messages.
YES_EMOJI = "✅"
NO_EMOJI = "❌"

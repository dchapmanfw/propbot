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

# Grace period after close_time before unresolved closed bets are auto-refunded (e.g. 24h).
UNRESOLVED_REFUND_AFTER: str = os.getenv("UNRESOLVED_REFUND_AFTER", "24h")

# Optional: restrict all bot commands and reactions to this channel ID (right-click channel → Copy Channel ID).
_allowed_channel_raw = os.getenv("ALLOWED_CHANNEL_ID", "").strip()
if _allowed_channel_raw:
    try:
        ALLOWED_CHANNEL_ID: int | None = int(_allowed_channel_raw)
    except ValueError as exc:
        raise SystemExit(
            f"ALLOWED_CHANNEL_ID must be a numeric Discord channel ID, got {_allowed_channel_raw!r}"
        ) from exc
else:
    ALLOWED_CHANNEL_ID = None

# Emoji used for YES / NO reactions on bet messages.
YES_EMOJI = "✅"
NO_EMOJI = "❌"

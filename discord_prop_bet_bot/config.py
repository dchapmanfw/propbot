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

# LMSR liquidity parameter for prediction markets (higher = more stable prices).
DEFAULT_MARKET_LIQUIDITY: float = float(os.getenv("DEFAULT_MARKET_LIQUIDITY", "100"))

# Maximum coins spendable on a single prediction-market buy.
MAX_MARKET_TRADE_COINS: int = int(os.getenv("MAX_MARKET_TRADE_COINS", "50"))

# Optional: sync slash commands to one server for instant updates during development.
_dev_guild_raw = os.getenv("DEV_GUILD_ID", "").strip()
if _dev_guild_raw:
    try:
        DEV_GUILD_ID: int | None = int(_dev_guild_raw)
    except ValueError as exc:
        raise SystemExit(
            f"DEV_GUILD_ID must be a numeric Discord server ID, got {_dev_guild_raw!r}"
        ) from exc
else:
    DEV_GUILD_ID = None

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

# Optional: channel for live market-analysis embeds (right-click channel → Copy Channel ID).
_market_board_raw = os.getenv("MARKET_BOARD_CHANNEL_ID", "").strip()
if _market_board_raw:
    try:
        MARKET_BOARD_CHANNEL_ID: int | None = int(_market_board_raw)
    except ValueError as exc:
        raise SystemExit(
            "MARKET_BOARD_CHANNEL_ID must be a numeric Discord channel ID, "
            f"got {_market_board_raw!r}"
        ) from exc
else:
    MARKET_BOARD_CHANNEL_ID = None

# How long resolved/cancelled board posts remain before deletion (e.g. 24h).
MARKET_BOARD_RETENTION: str = os.getenv("MARKET_BOARD_RETENTION", "24h")

# Emoji used for YES / NO reactions on bet messages.
YES_EMOJI = "✅"
NO_EMOJI = "❌"

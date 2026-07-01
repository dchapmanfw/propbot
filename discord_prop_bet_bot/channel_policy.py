"""Channel restriction helpers for single-channel server mode."""

from config import ALLOWED_CHANNEL_ID


def is_allowed_channel(
    channel_id: int | None,
    *,
    allowed_id: int | None = None,
    parent_id: int | None = None,
) -> bool:
    """Return True if channel_id is permitted. Unset allowed ID means all channels.

    When a single channel is configured, threads created under that channel are
    also permitted (parent_id is the parent text channel's ID).
    """
    restriction = ALLOWED_CHANNEL_ID if allowed_id is None else allowed_id
    if restriction is None:
        return True
    if channel_id == restriction:
        return True
    if parent_id == restriction:
        return True
    return False


def allowed_channel_message(*, allowed_id: int | None = None) -> str:
    """User-facing hint naming the configured betting channel."""
    restriction = ALLOWED_CHANNEL_ID if allowed_id is None else allowed_id
    if restriction is None:
        return "This command is not available in this channel."
    return f"Please use <#{restriction}> for prop bets."

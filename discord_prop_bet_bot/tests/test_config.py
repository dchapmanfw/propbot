"""Tests for config loading edge cases."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_invalid_allowed_channel_id_exits():
    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "ALLOWED_CHANNEL_ID": "not-a-number"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "ALLOWED_CHANNEL_ID" in result.stderr

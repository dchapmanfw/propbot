"""Find and stop other bot.py processes before starting a new instance."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def bot_script_path() -> Path:
    return Path(__file__).resolve().parent / "bot.py"


def _command_line_matches_bot(cmdline: str, script: Path) -> bool:
    if not cmdline:
        return False
    normalized = cmdline.lower()
    script_str = str(script).lower()
    return script_str in normalized or script.name.lower() in normalized


def find_other_bot_pids(
    *,
    script: Path | None = None,
    current_pid: int | None = None,
) -> list[int]:
    """Return PIDs of python processes running bot.py, excluding current_pid."""
    script = script or bot_script_path()
    current_pid = current_pid if current_pid is not None else os.getpid()

    if sys.platform == "win32":
        pids = _find_windows(script, current_pid)
    else:
        pids = _find_unix(script, current_pid)

    return sorted(set(pids))


def _find_windows(script: Path, current_pid: int) -> list[int]:
    script_pattern = str(script).replace("'", "''")
    script_name = script.name.replace("'", "''")
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { "
        "$_.Name -match 'python' -and "
        "$_.CommandLine -and "
        f"$_.ProcessId -ne {current_pid} -and "
        f"($_.CommandLine -like '*{script_pattern}*' -or "
        f"$_.CommandLine -match '(?:^|\\s|\"){script_name}(?:\\s|$|\")') "
        "} | Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not enumerate bot processes on Windows: %s", exc)
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _find_unix(script: Path, current_pid: int) -> list[int]:
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not enumerate bot processes: %s", exc)
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, cmd = parts
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if pid == current_pid:
            continue
        if _command_line_matches_bot(cmd, script):
            pids.append(pid)
    return pids


def kill_pid(pid: int) -> bool:
    """Terminate a process by PID. Returns True if kill was attempted."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        logger.warning("No permission to kill PID %d", pid)
        return False


def kill_other_bot_instances(*, wait_seconds: float = 2.0) -> int:
    """Stop other running bot.py processes. Returns the number terminated."""
    script = bot_script_path()
    targets = find_other_bot_pids(script=script)
    if not targets:
        logger.info("No other bot instances found.")
        return 0

    killed = 0
    for pid in targets:
        logger.info("Stopping other bot instance (PID %d)", pid)
        if kill_pid(pid):
            killed += 1

    if wait_seconds > 0 and killed:
        time.sleep(wait_seconds)
        remaining = find_other_bot_pids(script=script)
        if remaining:
            logger.warning(
                "After shutdown wait, %d bot process(es) still running: %s",
                len(remaining),
                remaining,
            )

    logger.info("Stopped %d other bot instance(s).", killed)
    return killed

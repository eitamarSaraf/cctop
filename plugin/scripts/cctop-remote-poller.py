# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""cctop remote poller — fetches ~/.cctop/ data from remote machines via SSH.

Reads machine list from ~/.cctop/machines.json:
  [
    {"alias": "trex", "user": "eitamar"},
    {"alias": "ada"},
    {"alias": "aws-ec2", "user": "ubuntu"}
  ]

On startup, SSHes into each machine and ensures cctop-poller.py is running there
(bootstraps it if not). Then polls every POLL_INTERVAL seconds, copying all *.json
files from ~/.cctop/ into ~/.cctop/remote/<alias>/. The dashboard picks them up.

On shutdown, stops the remote pollers it started.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

STATUS_DIR = Path.home() / ".cctop"
REMOTE_DIR = STATUS_DIR / "remote"
MACHINES_CONFIG = STATUS_DIR / "machines.json"
POLL_INTERVAL = 5.0  # seconds between SSH fetches per machine
SSH_TIMEOUT = 8      # seconds for each SSH call

_shutdown = False
_remote_poller_pids: dict[str, int] = {}  # alias -> PID on remote machine


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def load_machines() -> list[dict]:
    """Load machine list from ~/.cctop/machines.json. Returns [] if missing."""
    try:
        return json.loads(MACHINES_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def fetch_remote_sessions(alias: str) -> dict[str, dict] | None:
    """
    SSH into <alias> and read all *.json files from ~/.cctop/.
    Returns dict mapping filename -> parsed json, or None on failure.
    """
    # Single SSH call: list and cat all *.json files atomically
    script = (
        "dir=$HOME/.cctop; "
        "[ -d \"$dir\" ] || exit 0; "
        "for f in \"$dir\"/*.json; do "
        "  [ -f \"$f\" ] || continue; "
        "  echo \"===FILE===$(basename $f)\"; "
        "  cat \"$f\"; "
        "  echo; "
        "done"
    )
    try:
        result = subprocess.run(
            ["ssh", "-o", f"ConnectTimeout={SSH_TIMEOUT}", alias, script],
            capture_output=True, text=True, timeout=SSH_TIMEOUT + 2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    files: dict[str, dict] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    for line in result.stdout.splitlines():
        if line.startswith("===FILE==="):
            if current_name and current_lines:
                try:
                    files[current_name] = json.loads("\n".join(current_lines))
                except json.JSONDecodeError:
                    pass
            current_name = line[len("===FILE==="):]
            current_lines = []
        else:
            if current_name is not None:
                current_lines.append(line)

    # flush last file
    if current_name and current_lines:
        try:
            files[current_name] = json.loads("\n".join(current_lines))
        except json.JSONDecodeError:
            pass

    return files


def write_remote_sessions(alias: str, files: dict[str, dict]) -> None:
    """Write fetched remote session files to ~/.cctop/remote/<alias>/."""
    machine_dir = REMOTE_DIR / alias
    machine_dir.mkdir(parents=True, exist_ok=True)

    # Track which files we wrote so we can remove stale ones
    written: set[str] = set()

    for fname, data in files.items():
        # Tag every session with its source machine
        data["_machine"] = alias
        out_path = machine_dir / fname
        try:
            fd, tmp = tempfile.mkstemp(dir=machine_dir, prefix=".tmp.")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, out_path)
            written.add(fname)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # Remove files that disappeared on the remote (session ended)
    for existing in machine_dir.glob("*.json"):
        if existing.name not in written:
            try:
                existing.unlink(missing_ok=True)
            except OSError:
                pass


def bootstrap_remote_poller(alias: str) -> int | None:
    """
    Ensure cctop-poller.py is running on <alias>.
    Returns the remote PID if started/already running, None on failure.

    Strategy:
    1. Check if a poller process is already running (pgrep)
    2. If not, start it via nohup in the background
    3. Return the PID so we can stop it on shutdown
    """
    script = r"""
export PATH="$HOME/.local/bin:$PATH"
POLLER="$HOME/code/cctop/plugin/scripts/cctop-poller.py"
[ -f "$POLLER" ] || exit 1

# Check if already running
existing=$(pgrep -f "cctop-poller.py" 2>/dev/null | head -1)
if [ -n "$existing" ]; then
    echo "RUNNING:$existing"
    exit 0
fi

# Start it
mkdir -p "$HOME/.cctop"
nohup uv run --script "$POLLER" >> "$HOME/.cctop/poller.log" 2>&1 &
echo "STARTED:$!"
"""
    try:
        result = subprocess.run(
            ["ssh", "-o", f"ConnectTimeout={SSH_TIMEOUT}", alias, script],
            capture_output=True, text=True, timeout=SSH_TIMEOUT + 2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        if line.startswith("RUNNING:") or line.startswith("STARTED:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
    return None


def stop_remote_poller(alias: str, pid: int) -> None:
    """Kill the remote poller process we started on <alias>."""
    script = f"kill {pid} 2>/dev/null || true"
    try:
        subprocess.run(
            ["ssh", "-o", f"ConnectTimeout={SSH_TIMEOUT}", alias, script],
            capture_output=True, timeout=SSH_TIMEOUT + 2,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def poll_machine(alias: str) -> bool:
    """Fetch and store sessions for one machine. Returns True on success."""
    files = fetch_remote_sessions(alias)
    if files is None:
        return False
    write_remote_sessions(alias, files)
    return True


def main() -> None:
    REMOTE_DIR.mkdir(parents=True, exist_ok=True)

    # Per-machine next-poll timestamps
    next_poll: dict[str, float] = {}
    # Track which machines we bootstrapped (so we can stop them on exit)
    bootstrapped: set[str] = set()

    try:
        # Bootstrap pass: start remote pollers on all configured machines
        machines = load_machines()
        for machine in machines:
            alias = machine.get("alias", "")
            if not alias:
                continue
            pid = bootstrap_remote_poller(alias)
            if pid:
                _remote_poller_pids[alias] = pid
                bootstrapped.add(alias)

        while not _shutdown:
            machines = load_machines()

            for machine in machines:
                alias = machine.get("alias", "")
                if not alias:
                    continue

                # Bootstrap any newly added machine
                if alias not in bootstrapped:
                    pid = bootstrap_remote_poller(alias)
                    if pid:
                        _remote_poller_pids[alias] = pid
                        bootstrapped.add(alias)

                now = time.monotonic()
                if now >= next_poll.get(alias, 0):
                    poll_machine(alias)
                    next_poll[alias] = time.monotonic() + POLL_INTERVAL

            # Remove stale remote dirs for machines no longer in config
            configured = {m.get("alias") for m in machines if m.get("alias")}
            if REMOTE_DIR.is_dir():
                for d in REMOTE_DIR.iterdir():
                    if d.is_dir() and d.name not in configured:
                        for f in d.glob("*.json"):
                            try:
                                f.unlink(missing_ok=True)
                            except OSError:
                                pass

            # Sleep in small increments to stay responsive to shutdown
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not _shutdown:
                time.sleep(0.1)

    finally:
        # Stop remote pollers we started
        for alias, pid in _remote_poller_pids.items():
            stop_remote_poller(alias, pid)


if __name__ == "__main__":
    main()

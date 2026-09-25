"""Start Coriqo Shield at login (macOS launchd user agent).

Without this, Shield is only running while someone remembers to start it, and
the browser extension drops records after 50 while it is off. The agent is
deliberately small: it runs as the user (no root), at low CPU and IO priority,
restarts only after a failure, and waits 30 s between restarts so a port that
is taken by something else costs a wake-up every 30 s, not a busy loop.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.coriqo.shield"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(ledger: Path, port: int, python: str | None = None,
                log_dir: Path | None = None) -> dict:
    log_dir = log_dir or Path.home() / ".byoai" / "shield"
    return {
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, "-m",
                             "byoai.integrations.shield", str(ledger),
                             "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "Nice": 5,
        "LowPriorityIO": True,
        "StandardOutPath": str(log_dir / "shield.log"),
        "StandardErrorPath": str(log_dir / "shield.log"),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def run(*, install: bool, ledger: str, port: int) -> int:
    if sys.platform != "darwin":
        print("The login item is macOS only for now. On Linux, run byoai-shield "
              "from a systemd user unit.", file=sys.stderr)
        return 1
    domain = f"gui/{os.getuid()}"
    path = plist_path()
    _launchctl("bootout", f"{domain}/{LABEL}")  # fine if it was not loaded
    if not install:
        path.unlink(missing_ok=True)
        print("Shield will no longer start at login.")
        return 0
    ledger_path = Path(ledger).expanduser().resolve()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = Path.home() / ".byoai" / "shield"
    log_dir.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build_plist(ledger_path, port)))
    result = _launchctl("bootstrap", domain, str(path))
    if result.returncode != 0:
        print(f"launchctl could not load the login item: {result.stderr.strip()}",
              file=sys.stderr)
        return 1
    print(f"Shield now starts at login and is running on http://127.0.0.1:{port}/shield\n"
          f"Ledger: {ledger_path}\nRemove with: byoai-shield --remove-login-item")
    return 0

"""Start Coriqo Shield at login, on macOS, Linux and Windows.

Without this, Shield is only running while someone remembers to start it, and
the browser extension drops records after 50 while it is off. Each platform
gets its own native mechanism, run as the user (never root/SYSTEM), at low
priority, restarting only after a failure and no more than every 30 s, so a
port taken by something else costs a wake-up every 30 s, not a busy loop.

    macOS    launchd user agent   ~/Library/LaunchAgents/com.coriqo.shield.plist
    Linux    systemd user unit    ~/.config/systemd/user/coriqo-shield.service
    Windows  Task Scheduler       task "Coriqo Shield" (at logon, current user)
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.coriqo.shield"          # launchd
UNIT = "coriqo-shield.service"       # systemd
TASK = "Coriqo Shield"               # Windows Task Scheduler
RESTART_SECONDS = 30


def _log_dir() -> Path:
    return Path.home() / ".byoai" / "shield"


def _argv(ledger: Path, port: int, python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "byoai.integrations.shield",
            str(ledger), "--port", str(port)]


# ------------------------------------------------------------------ macOS

def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(ledger: Path, port: int, python: str | None = None,
                log_dir: Path | None = None) -> dict:
    log_dir = log_dir or _log_dir()
    return {
        "Label": LABEL,
        "ProgramArguments": _argv(ledger, port, python),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": RESTART_SECONDS,
        "ProcessType": "Background",
        "Nice": 5,
        "LowPriorityIO": True,
        "StandardOutPath": str(log_dir / "shield.log"),
        "StandardErrorPath": str(log_dir / "shield.log"),
    }


# ------------------------------------------------------------------ Linux

def unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT


def _systemd_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def build_unit(ledger: Path, port: int, python: str | None = None,
               log_dir: Path | None = None) -> str:
    log = (log_dir or _log_dir()) / "shield.log"
    exec_start = " ".join(_systemd_quote(a) for a in _argv(ledger, port, python))
    return "\n".join([
        "[Unit]",
        "Description=Coriqo Shield",
        "",
        "[Service]",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        f"RestartSec={RESTART_SECONDS}",
        "Nice=5",
        "IOSchedulingClass=idle",
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


# ---------------------------------------------------------------- Windows

def windows_command(ledger: Path, port: int, python: str | None = None) -> str:
    """The command line the scheduled task runs. pythonw.exe has no console
    window; Shield redirects its own output to the log when it has no stdout."""
    exe = python or sys.executable
    pythonw = Path(exe).with_name("pythonw.exe")
    if pythonw.exists():
        exe = str(pythonw)
    parts = [exe, "-m", "byoai.integrations.shield", str(ledger), "--port", str(port)]
    return " ".join(f'"{a}"' for a in parts)


def windows_create_args(ledger: Path, port: int, python: str | None = None) -> list[str]:
    # /RL LIMITED = no elevation. /F replaces an earlier install of the task.
    return ["schtasks", "/Create", "/TN", TASK, "/SC", "ONLOGON", "/RL", "LIMITED",
            "/F", "/TR", windows_command(ledger, port, python)]


# ----------------------------------------------------------------- driver

def _run(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(args), capture_output=True, text=True)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(args), 127, "", str(exc))


def run(*, install: bool, ledger: str, port: int) -> int:
    if sys.platform == "darwin":
        return _macos(install, ledger, port)
    if sys.platform.startswith("linux"):
        return _linux(install, ledger, port)
    if sys.platform == "win32":
        return _windows(install, ledger, port)
    print(f"No login item for {sys.platform}; run byoai-shield from your "
          "system's own startup mechanism.", file=sys.stderr)
    return 1


def _prepare(ledger: str) -> Path:
    ledger_path = Path(ledger).expanduser().resolve()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    _log_dir().mkdir(parents=True, exist_ok=True)
    return ledger_path


def _done(port: int, ledger_path: Path, remove_hint: str) -> int:
    print(f"Shield now starts at login and is running on http://127.0.0.1:{port}/shield\n"
          f"Ledger: {ledger_path}\nRemove with: {remove_hint}")
    return 0


def _macos(install: bool, ledger: str, port: int) -> int:
    domain = f"gui/{os.getuid()}"
    path = plist_path()
    _run("launchctl", "bootout", f"{domain}/{LABEL}")  # fine if it was not loaded
    if not install:
        path.unlink(missing_ok=True)
        print("Shield will no longer start at login.")
        return 0
    ledger_path = _prepare(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build_plist(ledger_path, port)))
    result = _run("launchctl", "bootstrap", domain, str(path))
    if result.returncode != 0:
        print(f"launchctl could not load the login item: {result.stderr.strip()}",
              file=sys.stderr)
        return 1
    return _done(port, ledger_path, "byoai-shield --remove-login-item")


def _linux(install: bool, ledger: str, port: int) -> int:
    path = unit_path()
    if not install:
        _run("systemctl", "--user", "disable", "--now", UNIT)
        path.unlink(missing_ok=True)
        _run("systemctl", "--user", "daemon-reload")
        print("Shield will no longer start at login.")
        return 0
    ledger_path = _prepare(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_unit(ledger_path, port))
    _run("systemctl", "--user", "daemon-reload")
    result = _run("systemctl", "--user", "enable", "--now", UNIT)
    if result.returncode != 0:
        print("systemctl could not start the login item (is this a systemd system "
              f"with a user session?): {result.stderr.strip()}", file=sys.stderr)
        return 1
    return _done(port, ledger_path, "byoai-shield --remove-login-item")


def _windows(install: bool, ledger: str, port: int) -> int:
    if not install:
        _run("schtasks", "/End", "/TN", TASK)
        _run("schtasks", "/Delete", "/TN", TASK, "/F")
        print("Shield will no longer start at login.")
        return 0
    ledger_path = _prepare(ledger)
    result = _run(*windows_create_args(ledger_path, port))
    if result.returncode != 0:
        print(f"Task Scheduler could not create the login item: {result.stderr.strip()}",
              file=sys.stderr)
        return 1
    _run("schtasks", "/Run", "/TN", TASK)
    return _done(port, ledger_path, "byoai-shield --remove-login-item")

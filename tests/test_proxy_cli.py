"""Smoke tests for the agent-context-cache CLI (start/stop/status + helpers).

These exercise the pid/argument logic without binding a port or spawning a real
server — the pidfile is redirected to a tmp path so nothing touches ~/.byoai/.
"""

from __future__ import annotations

import argparse
import os

import pytest

from byoai.agent_context_cache import main as acc_main


@pytest.fixture(autouse=True)
def _isolated_pidfile(tmp_path, monkeypatch):
    monkeypatch.setattr(acc_main, "_BYOAI_HOME", str(tmp_path))
    monkeypatch.setattr(acc_main, "_PID_FILE", str(tmp_path / "proxy.pid"))
    monkeypatch.setattr(acc_main, "_LOG_FILE", str(tmp_path / "proxy.log"))
    monkeypatch.delenv("BYOAI_HOST", raising=False)
    monkeypatch.delenv("BYOAI_PORT", raising=False)


def test_display_url_maps_wildcard_to_localhost():
    assert acc_main._display_url("0.0.0.0", 8787) == "http://localhost:8787"
    assert acc_main._display_url("::", 9000) == "http://localhost:9000"
    assert acc_main._display_url("127.0.0.1", 8080) == "http://127.0.0.1:8080"


def test_resolve_host_port_precedence(monkeypatch):
    ns = argparse.Namespace(host=None, port=None)
    # defaults
    assert acc_main._resolve_host_port(ns) == ("0.0.0.0", 8787)
    # env
    monkeypatch.setenv("BYOAI_HOST", "127.0.0.1")
    monkeypatch.setenv("BYOAI_PORT", "9001")
    assert acc_main._resolve_host_port(ns) == ("127.0.0.1", 9001)
    # explicit args win over env
    ns.host, ns.port = "0.0.0.0", 8080
    assert acc_main._resolve_host_port(ns) == ("0.0.0.0", 8080)


def test_bare_namespace_without_flags_resolves_from_env():
    # The no-subcommand path has no host/port attributes at all.
    empty = argparse.Namespace()
    assert acc_main._resolve_host_port(empty) == ("0.0.0.0", 8787)


def test_running_pid_cleans_up_stale_pidfile():
    # A dead pid in the file must be treated as "not running" and removed.
    with open(acc_main._PID_FILE, "w") as f:
        f.write("2000000000")  # implausible live pid -> ProcessLookupError
    assert acc_main._running_pid() is None
    assert not os.path.exists(acc_main._PID_FILE)


def test_running_pid_none_without_file():
    assert acc_main._running_pid() is None


def test_status_reports_stopped_when_not_running(capsys):
    rc = acc_main.cli(["status"])
    assert rc == 1
    assert "stopped" in capsys.readouterr().out


def test_stop_is_noop_when_not_running(capsys):
    rc = acc_main.cli(["stop"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out


def test_status_reports_running_for_live_pid(capsys):
    # Our own pid is definitely alive; status should report it.
    with open(acc_main._PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    rc = acc_main.cli(["status", "--port", "8799"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"running (pid {os.getpid()})" in out
    assert "http://localhost:8799" in out

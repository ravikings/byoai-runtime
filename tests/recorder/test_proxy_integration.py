"""End-to-end: the /v1/messages proxy actually seals events when the
recorder is enabled, and stays inert (no ledger, no behavior change) when it
is not. Upstream is faked with httpx.MockTransport, matching the pattern in
tests/test_agent_context_cache_api.py.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from byoai.agent_context_cache import main as acc_main
from byoai.recorder import integration as recorder_integration
from byoai.recorder.verify import verify_ledger


class FakeRedis:
    def __init__(self):
        self._store = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, *args, **kwargs):
        self._store[key] = value

    async def incrby(self, key, amount=1):
        self._store[key] = str(int(self._store.get(key, "0")) + amount)
        return int(self._store[key])

    async def close(self):
        pass


def upstream_tool_use(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "model": "claude-x",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "ls"}}
            ],
        },
    )


@pytest.fixture
def recorder_client(monkeypatch, tmp_path):
    monkeypatch.setattr(acc_main, "r", FakeRedis())
    monkeypatch.setattr(acc_main.db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(acc_main, "BENCHMARK_SAMPLE_RATE", 0.0)

    ledger_dir = tmp_path / "recorder"
    monkeypatch.setenv("BYOAI_RECORDER_ENABLED", "1")
    monkeypatch.setenv("BYOAI_RECORDER_DIR", str(ledger_dir))
    recorder_integration.reset_recorder_for_tests()

    with TestClient(acc_main.app) as test_client:
        # lifespan startup replaces `http_client` with a real network client;
        # only override it once the app is up, matching
        # tests/test_agent_context_cache_api.py's use_mock_upstream pattern.
        acc_main.http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_tool_use))
        yield test_client, ledger_dir

    recorder_integration.reset_recorder_for_tests()


def test_non_streaming_tool_use_lands_in_ledger(recorder_client):
    client, ledger_dir = recorder_client

    res = client.post(
        "/v1/messages",
        json={
            "model": "claude-x",
            "messages": [{"role": "user", "content": "list files"}],
        },
        headers={"x-api-key": "test-key"},
    )

    assert res.status_code == 200

    report = verify_ledger(ledger_dir / "ledger.db")
    assert report.entries_checked >= 1
    assert not report.broken_links
    assert not report.gaps
    kinds = {e for e in _read_kinds(ledger_dir)}
    assert "tool_use" in kinds


def test_request_side_tool_result_pairs_with_prior_tool_use(recorder_client):
    client, ledger_dir = recorder_client

    # First turn: model asks for a tool call.
    res1 = client.post(
        "/v1/messages",
        json={"model": "claude-x", "messages": [{"role": "user", "content": "list files"}]},
        headers={"x-api-key": "test-key"},
    )
    assert res1.status_code == 200

    # Second turn: client supplies the tool_result for that call.
    res2 = client.post(
        "/v1/messages",
        json={
            "model": "claude-x",
            "messages": [
                {"role": "user", "content": "list files"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "a.py\nb.py",
                        }
                    ],
                },
            ],
        },
        headers={"x-api-key": "test-key"},
    )
    assert res2.status_code == 200

    report = verify_ledger(ledger_dir / "ledger.db")
    assert not report.broken_links
    assert "toolu_1" not in report.orphan_tool_results


def test_recorder_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(acc_main, "r", FakeRedis())
    monkeypatch.setattr(acc_main.db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(acc_main, "BENCHMARK_SAMPLE_RATE", 0.0)
    monkeypatch.delenv("BYOAI_RECORDER_ENABLED", raising=False)
    recorder_integration.reset_recorder_for_tests()

    with TestClient(acc_main.app) as client:
        acc_main.http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_tool_use))
        res = client.post(
            "/v1/messages",
            json={"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": "test-key"},
        )
        assert res.status_code == 200

    assert acc_main.get_recorder() is None
    recorder_integration.reset_recorder_for_tests()


def _read_kinds(ledger_dir):
    import sqlite3

    conn = sqlite3.connect(str(ledger_dir / "ledger.db"))
    try:
        rows = conn.execute("SELECT kind FROM agent_events").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]

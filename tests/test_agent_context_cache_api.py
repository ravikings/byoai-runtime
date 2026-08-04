"""End-to-end tests for the /v1/messages proxy route, confirming the
_apply_optimizer/_maybe_run_benchmark_sample extraction in main.py didn't
change observable behavior. The upstream Anthropic API is faked with
httpx.MockTransport (the same no-network pattern used in
tests/test_providers_http.py) rather than a new mocking dependency.
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.testclient import TestClient

from byoai.agent_context_cache import main as acc_main

LONG_TEXT = "y" * 2500


class FakeRedis:
    """In-memory stand-in for the subset of redis-py's async API main.py uses."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def exists(self, key: str) -> bool:
        return key in self.kv

    async def set(self, key: str, value) -> None:
        self.kv[key] = str(value)

    async def get(self, key: str):
        return self.kv.get(key)

    async def incrby(self, key: str, amount: int) -> None:
        self.kv[key] = str(int(self.kv.get(key, "0")) + amount)

    async def sismember(self, key: str, value: str) -> bool:
        return value in self.sets.get(key, set())

    async def sadd(self, key: str, value: str) -> None:
        self.sets.setdefault(key, set()).add(value)

    async def expire(self, key: str, seconds: int) -> None:
        pass

    async def close(self) -> None:
        pass


def upstream_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(acc_main, "r", FakeRedis())
    monkeypatch.setattr(acc_main.db, "DB_PATH", str(tmp_path / "test.db"))
    acc_main._local_session_hashes.clear()
    with TestClient(acc_main.app) as test_client:
        yield test_client
    acc_main._local_session_hashes.clear()


def use_mock_upstream(handler):
    acc_main.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_health_check(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_proxy_injects_cache_control_when_client_sends_none(client):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return upstream_ok(request)

    use_mock_upstream(handler)

    res = client.post(
        "/v1/messages",
        json={
            "model": "claude-x",
            "system": "You are a helpful assistant.",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-api-key": "test-key"},
    )

    assert res.status_code == 200
    sent_body = json.loads(captured[0].content)
    assert isinstance(sent_body["system"], list)
    assert sent_body["system"][-1]["cache_control"] == {"type": "ephemeral"}


def test_proxy_passes_through_cache_control_when_client_already_set_it(client):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return upstream_ok(request)

    use_mock_upstream(handler)

    client_system = [{"type": "text", "text": "You are a helpful assistant.", "cache_control": {"type": "ephemeral"}}]
    res = client.post(
        "/v1/messages",
        json={
            "model": "claude-x",
            "system": client_system,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-api-key": "test-key"},
    )

    assert res.status_code == 200
    sent_body = json.loads(captured[0].content)
    # Untouched: exactly the one breakpoint the client set, not two.
    assert sent_body["system"] == client_system


def test_proxy_dedups_repeated_text_within_one_session_over_two_calls(client):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return upstream_ok(request)

    use_mock_upstream(handler)

    payload = {
        "model": "claude-x",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": LONG_TEXT}]},
        ],
    }
    headers = {"x-api-key": "test-key", "x-byoai-session-id": "one-session"}

    client.post("/v1/messages", json=payload, headers=headers)
    client.post("/v1/messages", json=payload, headers=headers)

    first_sent = json.loads(captured[0].content)
    second_sent = json.loads(captured[1].content)
    assert first_sent["messages"][0]["content"][0]["text"] == LONG_TEXT
    assert "Duplicate file snapshot detected" in second_sent["messages"][0]["content"][0]["text"]

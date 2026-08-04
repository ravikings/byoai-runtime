"""Tests for the optional BYOAI_PROXY_TOKEN shared-secret gate.

The gate lets the proxy be exposed through a public tunnel (ngrok/Cloudflare)
for a remote client without becoming an open relay. It accepts the token via an
`x-byoai-proxy-token` header or a leading URL path segment (so it works with
clients that can only set a base URL). Upstream Anthropic is faked with
httpx.MockTransport — no network.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from byoai.agent_context_cache import main as acc_main

TOKEN = "s3cret-token"


class FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def exists(self, key):
        return key in self.kv

    async def set(self, key, value):
        self.kv[key] = str(value)

    async def get(self, key):
        return self.kv.get(key)

    async def incrby(self, key, amount):
        self.kv[key] = str(int(self.kv.get(key, "0")) + amount)

    async def sismember(self, key, value):
        return value in self.sets.get(key, set())

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def expire(self, key, seconds):
        pass

    async def close(self):
        pass


def _upstream_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(acc_main, "r", FakeRedis())
    monkeypatch.setattr(acc_main.db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(acc_main, "BENCHMARK_SAMPLE_RATE", 0.0)
    acc_main._session_hash_store.fallback._sessions.clear()
    with TestClient(acc_main.app) as c:
        # Set the mock upstream AFTER lifespan startup, which otherwise
        # overwrites http_client with a real client.
        acc_main.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_upstream_ok))
        yield c
    acc_main._session_hash_store.fallback._sessions.clear()


_PAYLOAD = {"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]}
_KEY = {"x-api-key": "test-key"}


def test_no_token_configured_is_open(client, monkeypatch):
    monkeypatch.setattr(acc_main, "PROXY_TOKEN", "")
    res = client.post("/v1/messages", json=_PAYLOAD, headers=_KEY)
    assert res.status_code == 200


def test_token_set_rejects_missing_credential(client, monkeypatch):
    monkeypatch.setattr(acc_main, "PROXY_TOKEN", TOKEN)
    res = client.post("/v1/messages", json=_PAYLOAD, headers=_KEY)
    assert res.status_code == 401
    assert res.json()["error"]["type"] == "authentication_error"


def test_token_set_rejects_wrong_credential(client, monkeypatch):
    monkeypatch.setattr(acc_main, "PROXY_TOKEN", TOKEN)
    res = client.post(
        "/v1/messages", json=_PAYLOAD, headers={**_KEY, "x-byoai-proxy-token": "nope"}
    )
    assert res.status_code == 401


def test_token_accepted_via_header(client, monkeypatch):
    monkeypatch.setattr(acc_main, "PROXY_TOKEN", TOKEN)
    res = client.post(
        "/v1/messages", json=_PAYLOAD, headers={**_KEY, "x-byoai-proxy-token": TOKEN}
    )
    assert res.status_code == 200


def test_token_accepted_via_path_prefix(client, monkeypatch):
    monkeypatch.setattr(acc_main, "PROXY_TOKEN", TOKEN)
    # Simulates ANTHROPIC_BASE_URL=https://<host>/<token> — the client appends
    # /v1/messages, so the proxy sees /<token>/v1/messages.
    res = client.post(f"/{TOKEN}/v1/messages", json=_PAYLOAD, headers=_KEY)
    assert res.status_code == 200


def test_wrong_path_prefix_is_rejected(client, monkeypatch):
    monkeypatch.setattr(acc_main, "PROXY_TOKEN", TOKEN)
    res = client.post("/wrong-token/v1/messages", json=_PAYLOAD, headers=_KEY)
    assert res.status_code == 401


def test_health_is_exempt_from_gate(client, monkeypatch):
    monkeypatch.setattr(acc_main, "PROXY_TOKEN", TOKEN)
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_root_is_exempt_from_gate(client, monkeypatch):
    monkeypatch.setattr(acc_main, "PROXY_TOKEN", TOKEN)
    res = client.get("/")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_root_supports_head(client):
    # The 404 that motivated the root route came from a bare `HEAD /` probe.
    res = client.head("/")
    assert res.status_code == 200

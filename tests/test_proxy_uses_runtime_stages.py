"""Step-3 seam test: prove the /v1/messages proxy handler routes request-side
optimization through the extracted runtime primitives
(``byoai.stages.PromptCacheInjection`` and ``byoai.stages.SessionDedup``)
rather than the legacy inline functions.

Reuses the no-network mock-upstream + fake-redis pattern from
tests/test_agent_context_cache_api.py.
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.testclient import TestClient

from byoai.agent_context_cache import main as acc_main
from byoai import stages as stages_mod


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


@pytest.fixture(autouse=True)
def _restore_proxy_globals():
    """Guard against the known cross-test state-leak hazard: save/restore any
    proxy globals a test (or the live path) may mutate."""
    saved_r = acc_main.r
    acc_main._session_hash_store.fallback._sessions.clear()
    yield
    acc_main.r = saved_r
    acc_main._session_hash_store.fallback._sessions.clear()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(acc_main, "r", FakeRedis())
    monkeypatch.setattr(acc_main.db, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(acc_main.app) as test_client:
        yield test_client


def use_mock_upstream(handler):
    acc_main.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _capture_upstream():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return upstream_ok(request)

    use_mock_upstream(handler)
    return captured


def test_handler_routes_through_both_runtime_primitives(client, monkeypatch):
    """A normal POST must invoke both PromptCacheInjection.inject and
    SessionDedup.optimize on the live request path."""
    inject_calls: list[dict] = []
    optimize_calls: list[str] = []

    orig_inject = stages_mod.PromptCacheInjection.inject

    def spy_inject(body):
        inject_calls.append(body)
        return orig_inject(body)

    monkeypatch.setattr(
        stages_mod.PromptCacheInjection, "inject", staticmethod(spy_inject)
    )

    orig_optimize = stages_mod.SessionDedup.optimize

    async def spy_optimize(self, body, session_id):
        optimize_calls.append(session_id)
        return await orig_optimize(self, body, session_id)

    monkeypatch.setattr(stages_mod.SessionDedup, "optimize", spy_optimize)

    captured = _capture_upstream()

    res = client.post(
        "/v1/messages",
        json={
            "model": "claude-x",
            "system": "You are a helpful assistant.",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-api-key": "test-key", "x-byoai-session-id": "sess-1"},
    )

    assert res.status_code == 200
    # Both runtime primitives were exercised on the live path.
    assert len(inject_calls) == 1
    # derive_session_id prefixes the header-supplied id.
    assert optimize_calls == ["hdr:sess-1"]
    # And the injection actually took effect end-to-end (client sent no
    # cache_control, so the stage upgraded the system prompt).
    sent_body = json.loads(captured[0].content)
    assert isinstance(sent_body["system"], list)
    assert sent_body["system"][-1]["cache_control"] == {"type": "ephemeral"}


def test_handler_injection_is_noop_when_client_already_set_cache_control(
    client, monkeypatch
):
    """When the client already manages cache_control, routing through
    PromptCacheInjection.inject must not add any breakpoint."""
    inject_calls: list[dict] = []
    orig_inject = stages_mod.PromptCacheInjection.inject

    def spy_inject(body):
        inject_calls.append(body)
        return orig_inject(body)

    monkeypatch.setattr(
        stages_mod.PromptCacheInjection, "inject", staticmethod(spy_inject)
    )

    captured = _capture_upstream()

    client_system = [
        {
            "type": "text",
            "text": "You are a helpful assistant.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
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
    # inject ran on the live path...
    assert len(inject_calls) == 1
    # ...but produced no new breakpoint: exactly the client's one marker,
    # forwarded unchanged.
    sent_body = json.loads(captured[0].content)
    assert sent_body["system"] == client_system
    assert stages_mod._count_cache_control_markers(sent_body) == 1

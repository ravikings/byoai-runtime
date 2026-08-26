"""Seam B on the live ``/v1/messages`` path.

``tests/recorder/test_proxy_gate.py`` covers the enforcement library. This file
covers the wiring: that the proxy handler actually consults it, on both the
buffered and the streaming branch, and does nothing when it is switched off.

No network — the upstream is an ``httpx.MockTransport``, reusing the pattern in
``tests/test_proxy_uses_runtime_stages.py``.
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.testclient import TestClient

from byoai.agent_context_cache import main as acc_main
from byoai.recorder.denial_latch import DenialLatch, use_denial_latch
from byoai.recorder.mandate import MODEL_MESSAGE, MandateGate, Posture
from byoai.recorder.proxy_gate import clear_proxy_gates, register_proxy_gate

_AGENT = "agt_proxy_test"


class FakeRedis:
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


def seeded_gate() -> MandateGate:
    async def never_called(_etag):  # pragma: no cover - decide does no I/O
        raise AssertionError("the decide path must not refresh")

    gate = MandateGate(never_called, agent_id=_AGENT)
    gate.apply_snapshot(
        {
            "mandate_version_id": "mv_1",
            "allowed_tools": ["search"],
            "status": "approved",
            "mandate_enforcement": "enforce",
            "enforcement_posture": Posture.FAIL_OPEN,
            "max_staleness_s": 600,
        }
    )
    return gate


@pytest.fixture
def client(monkeypatch, tmp_path):
    saved_r = acc_main.r
    monkeypatch.setattr(acc_main, "r", FakeRedis())
    monkeypatch.setattr(acc_main.db, "DB_PATH", str(tmp_path / "test.db"))
    acc_main._session_hash_store.fallback._sessions.clear()
    with TestClient(acc_main.app) as test_client, use_denial_latch(DenialLatch()):
        yield test_client
    acc_main.r = saved_r
    acc_main._session_hash_store.fallback._sessions.clear()
    clear_proxy_gates()


@pytest.fixture
def enforcing(monkeypatch):
    monkeypatch.setenv("BYOAI_PROXY_ENFORCEMENT", "1")
    clear_proxy_gates()
    register_proxy_gate(seeded_gate())
    yield
    clear_proxy_gates()


def use_mock_upstream(handler):
    acc_main.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def tool_use_response(name: str) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "One moment."},
            {"type": "tool_use", "id": "toolu_1", "name": name, "input": {"q": "x"}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def post(client: TestClient, *, session: str = "sess-1", stream: bool = False):
    return client.post(
        "/v1/messages",
        json={
            "model": "claude-x",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        },
        headers={"x-api-key": "test-key", "x-byoai-session-id": session},
    )


# -- buffered ----------------------------------------------------------------


def test_allowed_tool_use_reaches_the_agent(client, enforcing):
    use_mock_upstream(lambda _r: httpx.Response(200, json=tool_use_response("search")))

    body = post(client).json()

    assert [b["type"] for b in body["content"]] == ["text", "tool_use"]
    assert body["stop_reason"] == "tool_use"


def test_denied_tool_use_never_reaches_the_agent(client, enforcing):
    use_mock_upstream(
        lambda _r: httpx.Response(200, json=tool_use_response("wire_transfer"))
    )

    body = post(client).json()

    assert [b["type"] for b in body["content"]] == ["text", "tool_result"]
    assert "wire_transfer" not in json.dumps(body)
    assert body["content"][-1]["content"] == [{"type": "text", "text": MODEL_MESSAGE}]
    assert body["stop_reason"] == "end_turn"


def test_enforcement_off_forwards_the_tool_use_unchanged(client, monkeypatch):
    monkeypatch.setenv("BYOAI_PROXY_ENFORCEMENT", "0")
    upstream = tool_use_response("wire_transfer")
    use_mock_upstream(lambda _r: httpx.Response(200, json=upstream))

    body = post(client).json()

    assert body == upstream


# -- streaming ---------------------------------------------------------------


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def stream_frames(name: str) -> list[bytes]:
    return [
        sse("message_start", {"type": "message_start", "message": {"id": "msg_1"}}),
        sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": name},
            },
        ),
        sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q":"x"}'},
            },
        ),
        sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        sse(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
        ),
        sse("message_stop", {"type": "message_stop"}),
    ]


def use_mock_stream(name: str):
    frames = stream_frames(name)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"".join(frames)),
            headers={"content-type": "text/event-stream"},
        )

    use_mock_upstream(handler)
    return frames


def test_streamed_allow_is_forwarded_byte_identical(client, enforcing):
    frames = use_mock_stream("search")
    assert post(client, stream=True).content == b"".join(frames)


def test_streamed_denial_withholds_the_block(client, enforcing):
    use_mock_stream("wire_transfer")

    out = post(client, session="sess-stream", stream=True).content

    assert b"wire_transfer" not in out
    assert b'"type": "tool_use"' not in out
    assert MODEL_MESSAGE.encode() in out
    assert b'"stop_reason":"end_turn"' in out


# -- the OpenAI-compat bridge is enforced too ---------------------------------
#
# This branch returns long before the Anthropic path's enforcement point, so it
# used to be a silent bypass: enforcement on, traffic routed through a compat
# model, no refusals and no warning. Worth a test of its own precisely because
# the failure was invisible — nothing errored, the tool simply ran.


def _openai_tool_call_response(tool: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": tool, "arguments": '{"q": "x"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _route_compat(monkeypatch, payload: dict) -> None:
    async def fake_forward(_client, _base, _key, _body):
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(acc_main, "OPENAI_COMPAT_BASE_URL", "http://upstream.invalid")
    monkeypatch.setattr(acc_main, "OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.setattr(acc_main.openai_compat, "forward_to_openai_compatible", fake_forward)
    monkeypatch.setattr(acc_main, "OPENAI_COMPAT_MODELS", {"gpt-4o"})


def test_openai_compat_bridge_denies_an_out_of_mandate_tool(client, enforcing, monkeypatch):
    _route_compat(monkeypatch, _openai_tool_call_response("wire_transfer"))
    body = {"model": "gpt-4o", "max_tokens": 16, "messages": [{"role": "user", "content": "go"}]}
    resp = client.post("/v1/messages", json=body)
    assert resp.status_code == 200
    blocks = resp.json()["content"]
    assert not [b for b in blocks if b.get("type") == "tool_use"], (
        "a denied tool_use reached the agent through the OpenAI-compat bridge"
    )
    assert any(MODEL_MESSAGE in json.dumps(b) for b in blocks)


def test_openai_compat_bridge_passes_an_in_mandate_tool_through(client, enforcing, monkeypatch):
    _route_compat(monkeypatch, _openai_tool_call_response("search"))
    body = {"model": "gpt-4o", "max_tokens": 16, "messages": [{"role": "user", "content": "go"}]}
    resp = client.post("/v1/messages", json=body)
    assert resp.status_code == 200
    tools = [b for b in resp.json()["content"] if b.get("type") == "tool_use"]
    assert [t["name"] for t in tools] == ["search"]

"""Shared test wiring for exercising MockCoriqo over a real ASGI transport."""

from __future__ import annotations

import time
import uuid

import httpx
from starlette.testclient import TestClient

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.schema import EVENT_SCHEMA_VERSION, AgentEvent, EventKind

from .mock_coriqo import MockCoriqo


def make_event(
    device_id: str,
    session_id: str = "sess_1",
    kind: EventKind = EventKind.TOOL_USE,
    *,
    payload: dict | None = None,
    tool_use_id: str | None = None,
    tool_name: str | None = "Bash",
) -> AgentEvent:
    payload = {"command": "ls -la"} if payload is None else payload
    return AgentEvent(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id="evt_" + uuid.uuid4().hex,
        device_id=device_id,
        session_id=session_id,
        seq=0,  # placeholder; the ledger assigns the real seq
        kind=kind.value,
        ts_device="2026-08-10T12:00:00.000000Z",
        ts_monotonic_ns=time.monotonic_ns(),
        tool_use_id=tool_use_id or ("toolu_" + uuid.uuid4().hex[:8]),
        tool_name=tool_name,
        payload=payload,
        payload_hash=sha256_hex(canonicalize(payload)),
        model="claude-opus-4-20250514",
        provider="anthropic",
    )


def asgi_client(mock: MockCoriqo, *, base_url: str = "http://mock-coriqo.test") -> httpx.Client:
    """A real httpx.Client (well, an httpx.Client subclass) wired to the
    mock's ASGI app — no sockets.

    The installed httpx version's ``ASGITransport`` only implements the
    async request path (``handle_async_request``), so a plain sync
    ``httpx.Client(transport=httpx.ASGITransport(...))`` raises
    ``AttributeError`` the moment it's used. Starlette's ``TestClient`` is a
    thin ``httpx.Client`` subclass that bridges sync calls onto an ASGI app
    correctly, and satisfies the ``http_client: httpx.Client`` parameter
    both ``enroll()`` and ``Shipper`` accept.

    Entering it as a context manager runs the ASGI app's lifespan
    startup/shutdown — MockCoriqo has none today, but callers should still
    use it via ``with`` so that stays true if it ever grows any.
    """
    return TestClient(mock.app, base_url=base_url)

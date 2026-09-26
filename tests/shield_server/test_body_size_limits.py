"""The security floor's request-size cap: enforced on the actual bytes read
off the wire (chunked or not), and on the decompressed size of a gzip body —
never buffered, decompressed or stored in full first."""

from __future__ import annotations

import gzip

import httpx
import pytest

from byoai.shield_server.app import MAX_BODY_BYTES, BodySizeLimitMiddleware


@pytest.fixture
async def async_client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_chunked_oversize_body_with_no_content_length_is_413(async_client):
    """A body sent as an async generator carries no Content-Length — httpx
    streams it chunk by chunk, exactly like a real chunked POST — so the old
    Content-Length-only check would never see it coming."""
    chunk = b"0" * (1024 * 1024)  # 1 MiB
    chunks_needed = (MAX_BODY_BYTES // len(chunk)) + 2

    async def body():
        for _ in range(chunks_needed):
            yield chunk

    r = await async_client.post("/v1/enroll", content=body())
    assert r.status_code == 413


async def test_body_at_or_under_the_cap_is_not_refused_for_size(async_client):
    """A body under the cap must not be rejected by the size guard — it can
    still fail for other reasons (bad token here), but never with 413."""
    r = await async_client.post("/v1/enroll", json={"public_key": "x", "token": "bogus"})
    assert r.status_code != 413


async def test_small_gzip_that_inflates_past_the_cap_is_413(async_client):
    """A classic decompression bomb: a few KB on the wire, tens of MB once
    inflated. The compressed body itself is tiny — well under the streaming
    cap — so this must be caught by the DECOMPRESSED-size guard, not the
    wire-size one."""
    huge = b"0" * (20 * 1024 * 1024)  # 20 MiB of a single repeated byte
    payload = gzip.compress(huge)
    assert len(payload) < 1024 * 1024  # confirms this is a small body on the wire

    r = await async_client.post(
        "/v1/shield/policy",
        content=payload,
        headers={
            "content-type": "application/json", "content-encoding": "gzip",
            "x-coriqo-device": "dev_doesnotexist", "x-coriqo-signature": "ed25519:bogus",
        },
    )
    assert r.status_code == 413


async def test_no_second_response_start_once_the_app_already_began_responding():
    """A handler that starts its response before it finishes draining the
    body (unusual, but not disallowed by ASGI) must not get a second,
    contradictory ``http.response.start`` stapled on once the size guard
    trips mid-stream — that would corrupt the response. Once the app has
    genuinely started, the middleware lets it finish rather than answering
    on its behalf.
    """
    sent: list[dict] = []

    async def small_app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        while True:
            message = await receive()
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break
        await send({"type": "http.response.body", "body": b"done", "more_body": False})

    middleware = BodySizeLimitMiddleware(small_app, max_bytes=5)

    receive_calls = {"n": 0}

    async def receive():
        receive_calls["n"] += 1
        if receive_calls["n"] == 1:
            # Already over max_bytes=5 on the very first chunk.
            return {"type": "http.request", "body": b"123456", "more_body": False}
        return {"type": "http.disconnect"}  # unreachable: middleware synthesises this itself

    async def send(message) -> None:
        sent.append(message)

    await middleware({"type": "http"}, receive, send)

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 200  # the app's own response, not a 413
    assert sent[-1] == {"type": "http.response.body", "body": b"done", "more_body": False}

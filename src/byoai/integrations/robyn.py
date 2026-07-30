"""Robyn transport adapter — Rust-powered HTTP in front of the ByoAI runtime.

Registers the same transport dialect as the FastAPI integration (see
``byoai.transport``): POST ``/execute`` (JSON), POST ``/stream`` (SSE),
and a ``/ws`` WebSocket — execution is identical across transports.

Usage with an existing Robyn app::

    from robyn import Robyn
    from byoai import Runtime
    from byoai.integrations.robyn import attach

    app = Robyn(__file__)
    runtime = Runtime(llm={"provider": "openai", "model": "gpt-4o"})
    attach(app, runtime)          # adds /byoai/execute, /byoai/stream, /byoai/ws
    app.start(port=8080)

Or ``create_app(runtime)`` for a standalone service. Requires the ``robyn``
extra: ``pip install byoai-runtime[robyn]``.
"""

from __future__ import annotations

from typing import Any

from .. import _json as json

try:
    from robyn import Headers, Robyn, StreamingResponse, jsonify
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "byoai.integrations.robyn requires Robyn: pip install 'byoai-runtime[robyn]'"
    ) from exc

from ..errors import (
    AllProvidersFailedError,
    ByoAIError,
    PipelineNotFoundError,
    ProviderError,
    RateLimitError,
)
from ..runtime import Runtime
from ..transport import execute_payload, parse_payload, sse_stream, ws_reply


def attach(
    app: Robyn,
    runtime: Runtime,
    *,
    prefix: str = "/byoai",
    stream_media_type: str = "text/event-stream",
    stream_headers: dict[str, str] | None = None,
) -> Runtime:
    """Register ByoAI routes on an existing Robyn app and bind lifecycle.

    Adds ``POST {prefix}/execute``, ``POST {prefix}/stream`` (SSE) and a
    ``{prefix}/ws`` WebSocket, plus a shutdown handler closing the runtime's
    provider/cache/vector connections. ``stream_headers`` defaults to
    disabling proxy buffering (``Cache-Control: no-cache``,
    ``X-Accel-Buffering: no``) — pass ``{}`` to omit them entirely.
    """
    effective_stream_headers = (
        {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        if stream_headers is None
        else stream_headers
    )

    @app.post(f"{prefix}/execute")
    async def _execute(request):  # Robyn injects its Request
        try:
            payload = request.json()
        except Exception:
            return _error_response(400, "request body must be JSON")
        try:
            # Validate eagerly so a malformed payload is a 400, distinct from
            # execution-time failures.
            parse_payload(payload)
        except ByoAIError as exc:
            return _error_response(400, str(exc))
        try:
            result = await execute_payload(runtime, payload)
        except ByoAIError as exc:
            return _error_response(_error_status(exc), str(exc), headers=_error_headers(exc))
        return jsonify(result)

    @app.post(f"{prefix}/stream")
    async def _stream(request):
        try:
            payload = request.json()
        except Exception:
            return _error_response(400, "request body must be JSON")
        try:
            # Validate before streaming starts so bad requests get a real
            # status code; mid-stream errors become SSE error events instead.
            parse_payload(payload)
        except ByoAIError as exc:
            return _error_response(400, str(exc))
        return StreamingResponse(
            sse_stream(runtime, payload),
            media_type=stream_media_type,
            # Robyn's StreamingResponse expects its native Headers type — a
            # plain dict is truthy so `headers or Headers({})` keeps the dict
            # as-is, and Robyn's own SSE-default-header code then calls
            # `.set()` on it, which a plain dict doesn't have.
            headers=Headers(effective_stream_headers),
        )

    @app.websocket(f"{prefix}/ws")
    async def _ws(websocket):
        # One JSON payload per message; frames streamed back per token batch.
        # Dialect (frames/errors) is shared via transport.ws_reply.
        try:
            while True:
                raw = await websocket.receive_text()
                async for frame in ws_reply(runtime, raw):
                    await websocket.send_text(frame)
        except Exception as exc:  # ordinary client disconnects must not raise
            if not _is_disconnect(exc):
                raise

    async def _close() -> None:
        await runtime.close()

    app.shutdown_handler(_close)
    return runtime


def create_app(
    runtime: Runtime,
    *,
    prefix: str = "/byoai",
    healthz_path: str | None = "/healthz",
    **attach_kwargs: Any,
) -> Robyn:
    """A standalone Robyn app serving the runtime.

    Registers ``GET {healthz_path}`` by default; pass ``healthz_path=None``
    to skip it (e.g. if your host app already defines that route).
    """
    app = Robyn(__file__)
    attach(app, runtime, prefix=prefix, **attach_kwargs)

    if healthz_path is not None:
        @app.get(healthz_path)
        async def _healthz():
            return jsonify({"ok": True})

    return app


def _is_disconnect(exc: Exception) -> bool:
    """True for Robyn/websocket disconnect-shaped exceptions."""
    try:
        from robyn import WebSocketDisconnect

        if isinstance(exc, WebSocketDisconnect):
            return True
    except ImportError:  # pragma: no cover
        pass
    return isinstance(exc, ConnectionError) or "disconnect" in type(exc).__name__.lower()


def _error_status(exc: ByoAIError) -> int:
    """HTTP status for an execution-time runtime error.

    Upstream provider failures are gateway errors (429/502), an unknown
    pipeline is a 404, and anything else (middleware/stage failures, runtime
    misconfiguration surfacing at request time) keeps the historical 422.
    """
    if isinstance(exc, RateLimitError):
        return 429
    if isinstance(exc, (AllProvidersFailedError, ProviderError)):
        return 502
    if isinstance(exc, PipelineNotFoundError):
        return 404
    return 422


def _error_headers(exc: ByoAIError) -> dict[str, str] | None:
    """Echo the provider's Retry-After hint so well-behaved clients back off."""
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        return {"Retry-After": str(max(0, round(retry_after)))}
    return None


def _error_response(status: int, message: str, headers: dict[str, str] | None = None) -> Any:
    from robyn import Response

    return Response(
        status_code=status,
        headers={"Content-Type": "application/json", **(headers or {})},
        description=json.dumps({"error": message}),
    )

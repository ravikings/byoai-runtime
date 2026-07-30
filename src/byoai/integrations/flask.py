"""Flask integration — sync WSGI in front of the async ByoAI runtime.

Flask is WSGI/sync; :class:`~byoai.Runtime` (and its httpx-based providers)
is asyncio-native. This module bridges the two with a persistent background
event-loop thread owned by the attached app, so route handlers stay plain
``def`` — no ``flask[async]``/asgiref, no ``async def`` views, and (unlike a
fresh ``asyncio.run()`` per request) the provider's httpx connection pool
survives across requests instead of reconnecting every call.

Usage with an existing Flask app, built inside an application factory (see
the "Deploying with gunicorn" note below for why the factory matters here)::

    from flask import Flask, request, jsonify
    from byoai import Runtime
    from byoai.integrations.flask import attach, execute, stream_response

    def create_app():
        app = Flask(__name__)
        attach(app, Runtime(llm={"provider": "anthropic", "model": "claude-sonnet-5"}))

        @app.post("/ask")
        def ask():
            result = execute(request.get_json()["query"])
            return jsonify({"content": result.content, "cached": result.cached})

        @app.post("/ask/stream")
        def ask_stream():
            return stream_response(request.get_json()["query"])

        return app

Deploying with gunicorn: build the ``Runtime``/call ``attach()`` inside the
app factory, never at module import time, and never under ``gunicorn
--preload`` — preload forks the master process *after* this module's
background thread has started, and ``fork()`` doesn't carry other threads
into the child. Use ``gthread`` workers; ``gevent``/``eventlet`` are not
validated against this bridge.

Requires the ``flask`` extra: ``pip install byoai-runtime[flask]``.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from collections.abc import AsyncGenerator, Iterator
from typing import Any, TypeVar

from .. import _json as json

try:
    from flask import Flask, Response, current_app, stream_with_context
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "byoai.integrations.flask requires Flask: pip install 'byoai-runtime[flask]'"
    ) from exc

from ..errors import ByoAIError, ConfigurationError
from ..runtime import Runtime
from ..transport import chunk_to_dict, has_content
from ..types import ExecutionResult

logger = logging.getLogger(__name__)

_STATE_ATTR = "byoai"

T = TypeVar("T")


class _FlaskBridge:
    """Owns one persistent asyncio event loop, in a background thread, for
    the app's lifetime. Every ``Runtime`` call is dispatched onto it via
    ``run_coroutine_threadsafe`` so the provider's httpx client binds to a
    single loop instead of a fresh one per request (which would break
    connection pooling — httpx binds its transport lazily on first use)."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._loop = asyncio.new_event_loop()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_forever, name="byoai-flask-bridge", daemon=True
        )
        self._thread.start()

    def _run_forever(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Any) -> Any:
        if self._closed:
            raise ConfigurationError("byoai Flask bridge is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def run_stream(self, agen: AsyncGenerator[T, None]) -> Iterator[T]:
        """Pull one item at a time, blocking the calling (Flask request)
        thread until each is ready — one round trip to the bridge loop per
        chunk. Always closes ``agen`` on exit, including an early client
        disconnect (Werkzeug's ``stream_with_context`` throws ``GeneratorExit``
        in here) — otherwise the abandoned provider stream/connection is
        never finalized on the bridge's loop.
        """
        try:
            while True:
                try:
                    yield self.run(agen.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            try:
                self.run(agen.aclose())
            except Exception:
                logger.warning(
                    "byoai Flask bridge: failed to close stream generator", exc_info=True
                )

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self.runtime.aclose(), self._loop).result(
                timeout=timeout
            )
        except Exception:
            logger.warning("byoai Flask bridge: runtime close failed", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)


def attach(app: Flask, runtime: Runtime) -> Runtime:
    """Bind ``runtime`` to ``app`` via ``app.extensions`` (Flask's own
    extension-state slot) and start its background bridge thread.

    Idempotent: calling this again on an already-attached app (a duplicate
    call, or Werkzeug's debug-mode reloader re-executing module code) returns
    the existing runtime instead of starting a second bridge thread — the
    ``runtime`` passed on that second call is unused and gets closed
    immediately (its provider(s) already opened real connections in
    ``__init__``, so leaving it open would leak them for the rest of the
    process). Registers ``atexit`` cleanup — WSGI has no lifespan-shutdown
    protocol like ASGI's, so this is the best available hook; it won't fire
    on SIGKILL, but does on gunicorn's graceful SIGTERM path.
    """
    existing: _FlaskBridge | None = app.extensions.get(_STATE_ATTR)
    if existing is not None:
        if runtime is not existing.runtime:
            try:
                existing.run(runtime.aclose())
            except Exception:
                logger.warning(
                    "byoai Flask bridge: failed to close a redundant Runtime "
                    "passed to a duplicate attach() call",
                    exc_info=True,
                )
        return existing.runtime
    bridge = _FlaskBridge(runtime)
    app.extensions[_STATE_ATTR] = bridge
    atexit.register(bridge.close)
    return runtime


def _bridge(app: Flask | None) -> _FlaskBridge:
    target = app if app is not None else current_app
    bridge = target.extensions.get(_STATE_ATTR)
    if bridge is None:
        raise ConfigurationError(
            "no ByoAI runtime attached to this app — call "
            "byoai.integrations.flask.attach(app, runtime) in your app factory"
        )
    return bridge


def get_runtime(app: Flask | None = None) -> Runtime:
    """The attached ``Runtime`` — defaults to ``flask.current_app``; pass
    ``app=`` explicitly when calling outside a request/app context (e.g. a
    background job wrapped in ``with app.app_context(): ...``)."""
    return _bridge(app).runtime


def execute(input: Any, *, app: Flask | None = None, **kwargs: Any) -> ExecutionResult:
    """Sync wrapper for ``runtime.execute()`` — blocks the calling (Flask
    request) thread until the result is ready."""
    bridge = _bridge(app)
    return bridge.run(bridge.runtime.execute(input, **kwargs))


def stream_response(
    input: Any,
    *,
    app: Flask | None = None,
    media_type: str = "text/event-stream",
    headers: dict[str, str] | None = None,
    **execute_kwargs: Any,
) -> Response:
    """SSE ``Response`` streaming ``runtime.stream()`` chunks — same frame
    shape as the FastAPI/Robyn integrations (via ``transport.chunk_to_dict``).
    ``headers`` defaults to disabling proxy buffering; pass ``{}`` to omit.
    """
    bridge = _bridge(app)
    effective_headers = (
        {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        if headers is None
        else headers
    )

    def event_source() -> Iterator[str]:
        agen = bridge.runtime.stream(input, **execute_kwargs)
        try:
            for chunk in bridge.run_stream(agen):
                if has_content(chunk):
                    yield f"data: {json.dumps(chunk_to_dict(chunk))}\n\n"
        except ByoAIError as exc:
            # Headers are already on the wire; emit the same terminal error
            # event as the other transports rather than tearing the
            # connection down.
            yield f"data: {json.dumps({'error': str(exc), 'done': True})}\n\n"

    # stream_with_context is required: Flask tears down the request/app
    # context as soon as the view returns unless the generator is wrapped.
    return Response(
        stream_with_context(event_source()), mimetype=media_type, headers=effective_headers
    )

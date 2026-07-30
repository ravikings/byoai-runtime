"""FastAPI integration.

Wires a :class:`byoai.Runtime` into an existing FastAPI app with three pieces:

* :func:`attach` — binds runtime lifecycle (startup/shutdown) to the app and
  stores the runtime on ``app.state.byoai``.
* :func:`get_runtime` — a ``Depends``-compatible accessor for route handlers.
* :func:`stream_response` — wraps ``runtime.stream()`` in a Server-Sent-Events
  ``StreamingResponse`` for token streaming.

Usage with an existing app::

    from fastapi import Depends, FastAPI
    from byoai import Runtime
    from byoai.integrations.fastapi import attach, get_runtime, stream_response

    app = FastAPI()
    attach(app, Runtime(llm={"provider": "openai", "model": "gpt-4o"}))

    @app.post("/ask")
    async def ask(body: dict, runtime: Runtime = Depends(get_runtime)):
        result = await runtime.execute(body["query"], user_id=body.get("user_id"))
        return {"content": result.content, "cached": result.cached,
                "usage": result.usage.__dict__}

    @app.post("/ask/stream")
    async def ask_stream(body: dict, runtime: Runtime = Depends(get_runtime)):
        return stream_response(runtime, body["query"], user_id=body.get("user_id"))

Requires the ``fastapi`` extra: ``pip install byoai-runtime[fastapi]``.
"""

from __future__ import annotations

from typing import Any

from .. import _json as json

try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import StreamingResponse
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "byoai.integrations.fastapi requires FastAPI: pip install 'byoai-runtime[fastapi]'"
    ) from exc

from ..errors import ByoAIError, ConfigurationError
from ..runtime import Runtime
from ..transport import chunk_to_dict, ws_reply

_STATE_ATTR = "byoai"


def attach(app: FastAPI, runtime: Runtime) -> Runtime:
    """Bind the runtime to an app: stores it on ``app.state`` and registers a
    shutdown hook that closes provider/cache/vector connections.

    Works alongside an app's existing lifespan/startup handlers — it only adds
    an ``on_shutdown`` callback rather than replacing the lifespan.
    """
    setattr(app.state, _STATE_ATTR, runtime)
    # add_event_handler is gone from the FastAPI/Starlette app class itself in
    # current versions but survives (deprecated) on FastAPI's own APIRouter —
    # hasattr-based duck typing across that version split, so `target` can't
    # be given a real static type here.
    target: Any = app if hasattr(app, "add_event_handler") else app.router
    target.add_event_handler("shutdown", runtime.close)
    return runtime


def get_runtime(request: Request) -> Runtime:
    """FastAPI dependency: ``runtime: Runtime = Depends(get_runtime)``.

    Also works with a ``WebSocket`` passed directly (not through ``Depends``)
    since both expose ``.app.state`` — useful for fetching the runtime inside
    a websocket route before calling :func:`serve_websocket`. Typed as
    ``Request`` rather than ``Request | WebSocket`` because FastAPI's
    dependency-injection machinery cannot build a response field for a Union
    here; callers using the websocket form should pass it through as-is (it
    works at runtime) or ``cast`` it for their own type-checking.
    """
    runtime = getattr(request.app.state, _STATE_ATTR, None)
    if runtime is None:
        raise ConfigurationError(
            "no ByoAI runtime attached to this app — call "
            "byoai.integrations.fastapi.attach(app, runtime) at startup"
        )
    return runtime


def stream_response(
    runtime: Runtime,
    input: Any,
    *,
    media_type: str = "text/event-stream",
    headers: dict[str, str] | None = None,
    **execute_kwargs: Any,
) -> StreamingResponse:
    """SSE response streaming ``runtime.stream()`` chunks.

    Emits ``data: {"delta": "..."}`` events per token batch and a final
    ``data: {"done": true, "usage": {...}}`` event. ``headers`` defaults to
    disabling proxy buffering (``Cache-Control: no-cache``,
    ``X-Accel-Buffering: no``) — pass ``{}`` to omit them entirely.
    """
    effective_headers = (
        {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        if headers is None
        else headers
    )

    async def event_source():
        try:
            async for chunk in runtime.stream(input, **execute_kwargs):
                if chunk.done or chunk.delta:
                    yield f"data: {json.dumps(chunk_to_dict(chunk))}\n\n"
        except ByoAIError as exc:
            # Headers are already on the wire; a torn connection would leave the
            # client guessing. Emit the same terminal error event as
            # transport.sse_stream so all transports fail identically.
            yield f"data: {json.dumps({'error': str(exc), 'done': True})}\n\n"

    return StreamingResponse(event_source(), media_type=media_type, headers=effective_headers)


async def serve_websocket(runtime: Runtime, websocket: WebSocket) -> None:
    """Serve the shared WebSocket dialect on an accepted-or-new connection.

    Each client message is one JSON payload (see ``byoai.transport``); the
    response is a stream of JSON frames — ``{"delta": ...}`` per token batch,
    then ``{"done": true, "usage": {...}}``. Use inside your own route::

        @app.websocket("/ws")
        async def ws(websocket: WebSocket, rt: Runtime = Depends(get_runtime)):
            await serve_websocket(rt, websocket)
    """
    if websocket.client_state.name == "CONNECTING":
        await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            async for frame in ws_reply(runtime, raw):
                await websocket.send_text(frame)
    except WebSocketDisconnect:
        pass

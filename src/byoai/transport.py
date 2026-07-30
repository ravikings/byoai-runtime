"""Transport-neutral request/response shaping.

Every transport (FastAPI, Robyn, WebSocket, queue workers, future gRPC/MCP)
speaks the same payload dialect and produces the same result shape by
delegating here. Execution stays identical regardless of transport — the
transports differ only in framing.

Request payload (JSON object):

    {
      "input": <str | {"messages": [...]} | ...>,   # or "query"/"prompt"
      "pipeline": "name",          # optional
      "session_id": "...",         # optional
      "user_id": "...",            # optional
      "model": "...",              # optional per-request override
      "filters": {...},            # optional AST filter dialect
      "options": {...}             # optional provider options (temperature, ...)
    }

Result shape (non-streaming):

    {"content": ..., "cached": ..., "model": ..., "provider": ...,
     "usage": {"input_tokens": ..., "output_tokens": ..., "cost_usd": ...,
               "cache_read_tokens": ..., "cache_creation_tokens": ...},
     "request_id": ..., "finish_reason": ...}

``finish_reason`` mirrors Anthropic's ``stop_reason`` (``"tool_use"`` on a
tool-call turn, etc.) — omitted from a streaming chunk frame unless set.
``ExecutionResult.raw``/``StreamChunk.raw`` (the provider's raw response —
not JSON-safe for every adapter) are deliberately excluded from every shape
here; use the Python API directly if you need them.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from typing import TYPE_CHECKING, Any

from . import _json as json
from .errors import ByoAIError, ConfigurationError

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime

_INPUT_KEYS = ("input", "query", "prompt", "messages")


def parse_payload(payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Split a transport payload into (input, execute-kwargs)."""
    if not isinstance(payload, dict):
        raise ConfigurationError("payload must be a JSON object")
    input_value: Any = None
    for key in _INPUT_KEYS:
        value = payload.get(key)
        if value is not None:  # an explicit null must not mask a later key
            input_value = {"messages": value} if key == "messages" else value
            break
    if input_value is None:
        raise ConfigurationError(f"payload requires one of {_INPUT_KEYS}")
    kwargs: dict[str, Any] = {}
    for key in ("pipeline", "session_id", "user_id", "model", "filters"):
        if payload.get(key) is not None:
            kwargs[key] = payload[key]
    options = payload.get("options")
    if isinstance(options, dict):
        kwargs.update(options)
    return input_value, kwargs


def result_to_dict(result: Any) -> dict[str, Any]:
    # result.raw is deliberately excluded — it's a Python-API-only escape
    # hatch (e.g. an SDK response object for Bedrock/Vertex) that isn't
    # JSON-safe; don't add it here.
    return {
        "content": result.content,
        "cached": result.cached,
        "model": result.model,
        "provider": result.provider,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_usd": result.usage.cost_usd,
            "cache_read_tokens": result.usage.cache_read_tokens,
            "cache_creation_tokens": result.usage.cache_creation_tokens,
        },
        "request_id": result.context.request_id,
        "finish_reason": result.finish_reason,
    }


async def execute_payload(runtime: Runtime, payload: dict[str, Any]) -> dict[str, Any]:
    """One non-streaming execution: payload in, result dict out."""
    input_value, kwargs = parse_payload(payload)
    result = await runtime.execute(input_value, **kwargs)
    return result_to_dict(result)


def chunk_to_dict(chunk: Any) -> dict[str, Any]:
    """One streamed chunk as a JSON-safe dict (delta frame or final frame).

    The final frame carries the same fields as the non-streaming result dict
    (``cached``, ``provider``, ``model``, ``usage``, ``request_id``) so a
    streaming consumer isn't missing anything a non-streaming one gets.
    """
    if chunk.done:
        frame: dict[str, Any] = {"done": True, "cached": chunk.cached}
        if chunk.usage is not None:
            frame["usage"] = {
                "input_tokens": chunk.usage.input_tokens,
                "output_tokens": chunk.usage.output_tokens,
                "cost_usd": chunk.usage.cost_usd,
                "cache_read_tokens": chunk.usage.cache_read_tokens,
                "cache_creation_tokens": chunk.usage.cache_creation_tokens,
            }
        if chunk.model:
            frame["model"] = chunk.model
        if chunk.provider:
            frame["provider"] = chunk.provider
        if chunk.request_id:
            frame["request_id"] = chunk.request_id
        if chunk.finish_reason:
            frame["finish_reason"] = chunk.finish_reason
        return frame
    if chunk.tool_call is not None:
        frame = {"delta": chunk.delta, "tool_call": {"index": chunk.tool_call.index}}
        if chunk.tool_call.id is not None:
            frame["tool_call"]["id"] = chunk.tool_call.id
        if chunk.tool_call.name is not None:
            frame["tool_call"]["name"] = chunk.tool_call.name
        if chunk.tool_call.partial_json:
            frame["tool_call"]["partial_json"] = chunk.tool_call.partial_json
        return frame
    return {"delta": chunk.delta}


def has_content(chunk: Any) -> bool:
    """Whether a StreamChunk carries anything worth emitting as a frame —
    shared by every transport's SSE loop (this module's own ``stream_frames``,
    plus the FastAPI/Flask integrations' hand-rolled loops, which stream from
    ``runtime.stream()`` directly instead of a payload dict and so can't call
    ``stream_frames`` itself) so a new StreamChunk field never needs updating
    in more than one place to reach every transport.
    """
    return chunk.done or bool(chunk.delta) or chunk.tool_call is not None


async def stream_frames(
    runtime: Runtime, payload: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """Stream an execution as JSON-safe frames (transport adds its own framing)."""
    input_value, kwargs = parse_payload(payload)
    async for chunk in runtime.stream(input_value, **kwargs):
        if has_content(chunk):
            yield chunk_to_dict(chunk)


async def sse_stream(runtime: Runtime, payload: dict[str, Any]) -> AsyncGenerator[str, None]:
    """Stream an execution as Server-Sent-Events lines.

    Runtime errors raised mid-stream (after headers are on the wire) become a
    final ``data: {"error": ..., "done": true}`` event rather than a torn
    connection. Transports that can still return a status code should call
    :func:`parse_payload` eagerly first and map ``ByoAIError`` to 4xx.
    """
    try:
        async for frame in stream_frames(runtime, payload):
            yield f"data: {json.dumps(frame)}\n\n"
    except ByoAIError as exc:
        yield f"data: {json.dumps({'error': str(exc), 'done': True})}\n\n"


async def ws_reply(runtime: Runtime, raw_message: str) -> AsyncGenerator[str, None]:
    """Answer one WebSocket text message with a stream of JSON frame strings.

    Shared by every WebSocket transport so the message dialect (delta frames,
    final usage frame, ``{"error": ...}`` frames for bad JSON or runtime
    errors) cannot drift between integrations. The caller owns the socket:
    receive loop, sending each yielded string, and disconnect handling.
    """
    try:
        payload = json.loads(raw_message)
    except ValueError:
        yield json.dumps({"error": "message must be JSON"})
        return
    try:
        async for frame in stream_frames(runtime, payload):
            yield json.dumps(frame)
    except ByoAIError as exc:
        yield json.dumps({"error": str(exc), "done": True})

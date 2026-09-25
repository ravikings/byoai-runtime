"""MCP capture demo — what BYOAI records when an agent calls over MCP.

Answer to "what will we capture": run

    python examples/mcp_capture/server.py --http      # streamable HTTP :8800/mcp

then point any MCP client (Claude connector, MCP Inspector, or client.py in
this directory) at http://localhost:8800/mcp. Every signal lands in
``captures.jsonl`` and echoes to stderr as one-line state; this is a minimal
wiring of mcp_surface_spec.md §2 (behavior capture at the MCP surface).

Captured per interaction (the §2 schema, transitional):

  session/init    initialize handshake: client_info name+version, capabilities
  tools/list      which tool(s) a surface enumerated (even without calls)
  tool/call       tool choice (execute vs execute_stream), resolved payload,
                  identity overrides, live per-request stream
  request.*       runtime lifecycle: received → completed → provider.completed
                  (through the same middleware chain used by HTTP)
  progress        per-delta streaming telemetry (chars, delta count)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from byoai import Runtime
from byoai.integrations.shield import append_row, inspect_text, read_policy
from byoai.providers import FunctionProvider
from byoai.transport import execute_payload, stream_frames

try:
    from mcp.server import MCPServer as _MCPServerCls
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _MCPServerCls  # type: ignore[no-redef]

try:
    from mcp.server.mcpserver import Context as Context  # type: ignore
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import Context  # type: ignore[no-redef]

HERE = Path(__file__).parent
CAPTURES = HERE / "captures.jsonl"
POLICY = HERE / "policy.json"
_lock = asyncio.Lock()


def _input_facts(text: str) -> dict:
    """Privacy-first: rule hits, length and a keyed fingerprint of the tool
    input, plus a redacted preview only when the shield's keep_text is on."""
    return inspect_text(text, read_policy(POLICY))


async def capture(kind: str, **fields: object) -> None:
    rec = {"wall_clock": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **fields}
    async with _lock:
        append_row(CAPTURES, rec)
    print(f"[capture] {json.dumps(rec, default=str)}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- echo provider
async def _echo(messages, **options) -> str:
    await asyncio.sleep(0.05)
    last = messages[-1].content if messages else ""
    words = len(last.split()) or 1
    return f"echo: {words} words / {len(last)} chars received"


async def _echo_stream(messages, **options):
    async def gen():
        text = await _echo(messages, **options)
        for word in text.split(" "):
            yield word + " "
    async for chunk in gen():
        yield chunk


runtime = Runtime(
    providers=[FunctionProvider(_echo, name="echo", model="echo-1",
                                stream_fn=_echo_stream)],
    cache={"provider": "memory"},
)


async def _on_event(_name: str, kw: dict[str, object]) -> None:
    """Runtime lifecycle events, summarized (request.received/completed,
    cache.hit, provider.*). This is the same middleware chain HTTP transports
    already emit through — showing that MCP capture rides the same events."""
    ctx = kw.get("ctx")
    if ctx is None:
        return
    usage = getattr(ctx, "usage", None)
    await capture(
        "runtime." + _name,
        request_id=getattr(ctx, "request_id", None),
        user_id=getattr(ctx, "user_id", None),
        session_id=getattr(ctx, "session_id", None),
        input_chars=len(str(getattr(ctx, "input", "") or "")),
        model=getattr(ctx, "model", None),
        provider=getattr(ctx, "provider", None),
        cached=getattr(ctx, "cached", None),
        total_tokens=getattr(usage, "total_tokens", None) if usage else None,
        elapsed_ms=round(getattr(ctx, "elapsed_ms", 0) or 0, 2),
        extra={k: v for k, v in kw.items() if k not in ("ctx",)},
    )


# subscribe once to runtime lifecycle events (MCP shares the same chain as HTTP)
for _evt in ("request.received", "request.completed", "request.failed",
             "cache.hit", "provider.started", "provider.completed"):
    runtime.on(_evt, _on_event)


# ---------------------------------------------------------------- mcp server

server: Any = _MCPServerCls("byoai-capture-demo")


@server.tool(
    name="execute",
    description="Execute a request through the ByoAI runtime (echo backend in "
    "this demo). Same result shape as POST /execute on the HTTP transport.",
)
async def execute(
    input: str,
    ctx: Context | None = None,
    pipeline: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"input": input}
    for key in ("pipeline", "session_id", "user_id", "model"):
        if locals().get(key) is not None:  # noqa: B009 — introspection of tool args
            payload[key] = locals()[key]
    identity = _identity(ctx, payload)
    # Ties this call's result (or error) to it when calls overlap.
    call_id = uuid.uuid4().hex[:12]

    await capture(
        "tool.call",
        call_id=call_id,
        tool="execute",
        identity=identity,
        **_input_facts(input),
        args={k: v for k, v in (("pipeline", payload.get("pipeline")),
                                ("model", payload.get("model")))} ,
    )
    if input == "introspect":
        names = [a for a in dir(ctx) if not a.startswith("_")]
        dump: dict[str, Any] = {"ctx_attrs": names}
        for a in ("session", "request_context"):
            obj = getattr(ctx, a, None)
            if obj is not None:
                dump[a] = [x for x in dir(obj) if not x.startswith("_")][:60]
        return {"content": json.dumps(dump, indent=1)}

    t0 = time.perf_counter()
    try:
        result = await execute_payload(runtime, payload)
    except Exception as exc:  # noqa: BLE001
        await capture("tool.error", call_id=call_id, tool="execute", identity=identity,
                      error=str(exc), error_type=type(exc).__name__,
                      latency_ms=round((time.perf_counter() - t0) * 1000, 2))
        return {"error": str(exc), "error_type": type(exc).__name__}
    result = _slim(result)
    await capture("tool.result", call_id=call_id, tool="execute", identity=identity,
                  content_chars=len(str(result.get("content", ""))),
                  latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                  **{k: result.get(k) for k in ("cached", "model", "provider", "usage")})
    return result


@server.tool(
    name="execute_stream",
    description="Like execute but streams token deltas as progress "
    "notifications while returning the full result at the end.",
)
async def execute_stream(
    input: str,
    ctx: Context | None = None,
    pipeline: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"input": input}
    for key in ("pipeline", "session_id", "user_id", "model"):
        if locals().get(key) is not None:
            payload[key] = locals()[key]
    identity = _identity(ctx, payload)
    # Ties this call's result (or error) to it when calls overlap.
    call_id = uuid.uuid4().hex[:12]

    await capture(
        "tool.call",
        call_id=call_id,
        tool="execute_stream",
        identity=identity,
        **_input_facts(input),
        wants_stream=True,
    )
    t0 = time.perf_counter()
    parts: list[str] = []
    deltas = 0
    final: dict[str, Any] = {}
    try:
        async for frame in stream_frames(runtime, payload):
            if frame.get("done"):
                final = frame
                continue
            delta: str = frame.get("delta", "")
            if not delta:
                continue
            parts.append(delta)
            if ctx is not None:
                try:
                    await ctx.report_progress(len("".join(parts)), message=delta)
                except ValueError:
                    pass
            if deltas < 5:
                await capture("progress.delta", identity=identity,
                              tool="execute_stream", n=deltas + 1,
                              chars=len(delta))
            deltas += 1
    except Exception as exc:  # noqa: BLE001
        await capture("tool.error", call_id=call_id, tool="execute_stream", identity=identity,
                      error=str(exc), error_type=type(exc).__name__)
        return {"error": str(exc), "error_type": type(exc).__name__}
    result = _slim(final)
    await capture(  # one row per interaction: final
        "tool.result",
        call_id=call_id,
        tool="execute_stream",
        identity=identity,
        content_chars=len("".join(parts)),
        deltas=deltas,
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        **{k: result.get(k) for k in ("cached", "model", "provider", "usage")},
    )
    return result


def _slim(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    out.pop("raw", None)
    out.pop("request_id", None)
    return out


def _client_info(ctx: Context | None) -> dict[str, Any] | None:
    """Best-effort client identity from the MCP context (spec §2: which app the
    user actually lives in). Different SDK versions surface this on different
    attributes; try the plausible ones."""
    if ctx is None:
        return None
    session = getattr(ctx, "session", None)
    params = getattr(session, "client_params", None)  # initialize params, 2.x SDK
    info = getattr(params, "client_info", None)
    if info is None:  # older SDK layouts
        for probe in ("request_context", "session"):
            x = getattr(ctx, probe, None)
            info = getattr(x, "client_info", None) or getattr(x, "client", None)
            if info is not None:
                break
    if info is None:
        return None
    return {"name": getattr(info, "name", None), "version": getattr(info, "version", None)}


def _identity(ctx: Context | None, payload: dict[str, Any]) -> dict[str, Any]:
    """Identity captured at call time. In the real gateway (spec §1–2) this
    comes from auth (API key or OAuth claims) and client-supplied values are
    reconciled; here it's whatever the client told us, logged so override or
    spoof behavior stays visible instead of becoming an opaque failure."""
    info = _client_info(ctx)
    return {
        "user_id": payload.get("user_id"),
        "session_id": payload.get("session_id"),
        "client_name": info.get("name") if info else None,
        "client_version": info.get("version") if info else None,
    }


# ------------------------------------------------------------ run

if __name__ == "__main__":
    import os

    if "--http" in sys.argv:
        asyncio.run(
            server.run_streamable_http_async(
                host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8800")),
            )
        )
    else:
        asyncio.run(server.run_stdio_async())

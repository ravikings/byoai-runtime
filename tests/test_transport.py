from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from byoai import ConfigurationError, Runtime
from byoai.transport import execute_payload, parse_payload, sse_stream, stream_frames


def test_parse_payload_variants():
    assert parse_payload({"input": "hi"}) == ("hi", {})
    assert parse_payload({"query": "hi"}) == ("hi", {})
    assert parse_payload({"prompt": "hi"}) == ("hi", {})
    input_value, _ = parse_payload({"messages": [{"role": "user", "content": "hi"}]})
    assert input_value == {"messages": [{"role": "user", "content": "hi"}]}


def test_parse_payload_kwargs_and_options():
    _, kwargs = parse_payload(
        {
            "input": "hi",
            "pipeline": "legal",
            "user_id": "u1",
            "model": "m2",
            "filters": {"dept": "legal"},
            "options": {"temperature": 0.2},
        }
    )
    assert kwargs == {
        "pipeline": "legal",
        "user_id": "u1",
        "model": "m2",
        "filters": {"dept": "legal"},
        "temperature": 0.2,
    }


def test_parse_payload_missing_input_raises():
    with pytest.raises(ConfigurationError):
        parse_payload({"pipeline": "x"})


def test_parse_payload_explicit_null_does_not_mask_later_key():
    # a template body sending every known key, only one populated
    assert parse_payload({"input": None, "query": "real question"})[0] == "real question"
    with pytest.raises(ConfigurationError):
        parse_payload({"input": None, "query": None})


async def test_ws_reply_error_frames():
    import json

    from byoai.transport import ws_reply

    runtime = Runtime(providers=[FakeProvider()])
    frames = [json.loads(f) async for f in ws_reply(runtime, "not json{")]
    assert frames == [{"error": "message must be JSON"}]

    frames = [json.loads(f) async for f in ws_reply(runtime, json.dumps({"nope": 1}))]
    assert frames[0]["done"] is True and "error" in frames[0]

    frames = [json.loads(f) async for f in ws_reply(runtime, json.dumps({"input": "hi"}))]
    assert frames[-1]["done"] is True
    assert "".join(f.get("delta", "") for f in frames).strip() == "hello from fake"


async def test_sse_stream_error_becomes_event_not_exception():
    import json

    runtime = Runtime(providers=[FakeProvider()])
    lines = [line async for line in sse_stream(runtime, {"no_input_key": 1})]
    assert len(lines) == 1
    frame = json.loads(lines[0][len("data: ") :])
    assert frame["done"] is True and "error" in frame


async def test_execute_payload_result_shape():
    runtime = Runtime(providers=[FakeProvider()])
    result = await execute_payload(runtime, {"input": "hi"})
    assert result["content"] == "hello from fake"
    assert result["cached"] is False
    assert result["provider"] == "fake"
    assert result["usage"]["input_tokens"] == 10
    assert result["request_id"]


async def test_stream_frames_shape():
    runtime = Runtime(providers=[FakeProvider(reply="a b")])
    frames = [frame async for frame in stream_frames(runtime, {"input": "hi"})]
    assert frames[:-1] == [{"delta": "a "}, {"delta": "b "}]
    assert frames[-1]["done"] is True
    assert frames[-1]["usage"]["output_tokens"] == 5
    # Final frame carries the same fields as the non-streaming result dict —
    # a streaming consumer isn't missing cached/provider/request_id.
    assert frames[-1]["provider"] == "fake"
    assert frames[-1]["cached"] is False
    assert frames[-1]["request_id"]


async def test_stream_frames_short_circuit_marks_cached():
    from byoai.cache.memory import MemoryCache

    runtime = Runtime(providers=[FakeProvider(reply="x")], cache=MemoryCache())
    await runtime.execute("same q")  # populates the exact-match cache

    async def guard(ctx, call_next):
        ctx.model, ctx.provider = "cached-model", "cached-provider"
        ctx.short_circuit("short-circuited answer", cached=True)

    runtime.use(guard)
    frames = [frame async for frame in stream_frames(runtime, {"input": "anything"})]
    assert frames[0] == {"delta": "short-circuited answer"}
    assert frames[-1]["cached"] is True
    assert frames[-1]["model"] == "cached-model"
    assert frames[-1]["provider"] == "cached-provider"


async def test_stream_frames_carries_tool_call_chunks():
    # Regression: stream_frames()'s filter (`if chunk.done or chunk.delta:`)
    # silently dropped any chunk whose only content was a tool_call, since
    # such a chunk has delta="" (falsy) and done=False — the exact same class
    # of bug the provider adapters had before being fixed to emit tool_call
    # chunks in the first place. A forced tool_choice call streamed over
    # HTTP/SSE/WebSocket got zero tool_call frames even though the Python API
    # (runtime.stream() directly) worked correctly.
    from tests.conftest import ToolCallingProvider

    runtime = Runtime(providers=[ToolCallingProvider()])
    frames = [frame async for frame in stream_frames(runtime, {"input": "hi"})]
    tool_frames = [f for f in frames if "tool_call" in f]
    assert tool_frames == [
        {"delta": "", "tool_call": {"index": 0, "id": "toolu_1", "name": "answer"}},
        {"delta": "", "tool_call": {"index": 0, "partial_json": '{"a": 1}'}},
    ]
    assert frames[-1]["done"] is True


async def test_sse_stream_lines():
    runtime = Runtime(providers=[FakeProvider(reply="x")])
    lines = [line async for line in sse_stream(runtime, {"input": "hi"})]
    assert all(line.startswith("data: ") and line.endswith("\n\n") for line in lines)

"""Provider adapter tests against an httpx.MockTransport — no network."""

from __future__ import annotations

import httpx
import pytest

from byoai.errors import ProviderError, RateLimitError
from byoai.providers.openai_compat import OpenAICompatProvider
from byoai.types import Message

MESSAGES = [Message(role="user", content="hi")]


def make_provider(handler) -> OpenAICompatProvider:
    client = httpx.AsyncClient(
        base_url="https://mock.test/v1", transport=httpx.MockTransport(handler)
    )
    return OpenAICompatProvider(model="m", api_key="k", client=client)


async def test_empty_choices_raises_provider_error_not_index_error():
    def handler(request):
        return httpx.Response(200, json={"choices": [], "model": "m"})

    with pytest.raises(ProviderError) as excinfo:
        await make_provider(handler).complete(MESSAGES)
    assert "no choices" in str(excinfo.value)
    assert excinfo.value.retryable is False


async def test_http_date_retry_after_is_parsed():
    def handler(request):
        return httpx.Response(
            429,
            json={"error": {"message": "slow down"}},
            headers={"Retry-After": "Wed, 21 Oct 2100 07:28:00 GMT"},
        )

    with pytest.raises(RateLimitError) as excinfo:
        await make_provider(handler).complete(MESSAGES)
    # RFC 9110 HTTP-date form becomes a positive delay-from-now hint.
    assert excinfo.value.retry_after is not None
    assert excinfo.value.retry_after > 0
    assert excinfo.value.retryable is True


async def test_unparseable_retry_after_is_ignored():
    def handler(request):
        return httpx.Response(
            429,
            json={"error": {"message": "slow down"}},
            headers={"Retry-After": "soonish"},
        )

    with pytest.raises(RateLimitError) as excinfo:
        await make_provider(handler).complete(MESSAGES)
    assert excinfo.value.retry_after is None  # unparseable → no delay hint
    assert excinfo.value.retryable is True


async def test_numeric_retry_after_is_parsed():
    def handler(request):
        return httpx.Response(
            429, json={"error": {"message": "slow down"}}, headers={"Retry-After": "7"}
        )

    with pytest.raises(RateLimitError) as excinfo:
        await make_provider(handler).complete(MESSAGES)
    assert excinfo.value.retry_after == 7.0


async def test_malformed_response_body_raises_provider_error_not_json_error():
    # Regression: a 200 response that isn't valid JSON (e.g. a misconfigured
    # gateway/proxy returning an HTML error page) used to leak a raw
    # json.JSONDecodeError past the adapter instead of a ProviderError.
    def handler(request):
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(ProviderError) as excinfo:
        await make_provider(handler).complete(MESSAGES)
    assert "malformed response body" in str(excinfo.value)
    assert excinfo.value.retryable is False


async def test_valid_json_wrong_shape_raises_provider_error_not_attribute_error():
    # Regression: a 200 response that's valid JSON but not an object (a bare
    # list or null — e.g. a misconfigured gateway) parsed successfully, then
    # the adapter's next `data.get(...)` raised a raw AttributeError instead
    # of a ProviderError.
    def handler(request):
        return httpx.Response(200, json=[])

    with pytest.raises(ProviderError) as excinfo:
        await make_provider(handler).complete(MESSAGES)
    assert "not an object" in str(excinfo.value)
    assert excinfo.value.retryable is False


async def test_successful_completion_parses_usage():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    response = await make_provider(handler).complete(MESSAGES)
    assert response.content == "ok"
    assert response.usage.total_tokens == 4


async def test_metadata_option_is_stripped_not_forwarded():
    # Regression: provider_metadata= (Runtime.execute()'s Anthropic-shaped
    # top-level metadata field) landed in provider_options["metadata"] and was
    # forwarded verbatim into the request body via **options — several
    # OpenAI-compatible backends this adapter also fronts (Azure, vLLM, ...)
    # reject unrecognized top-level fields with a 400.
    captured = {}

    def handler(request):
        import json as stdlib_json

        captured["body"] = stdlib_json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )

    await make_provider(handler).complete(MESSAGES, metadata={"user_id": "u1"})
    assert "metadata" not in captured["body"]


async def test_stream_error_event_raises_instead_of_clean_done():
    def handler(request):
        body = (
            b'data: {"model": "m", "choices": [{"delta": {"content": "par"}}]}\n\n'
            b'data: {"error": {"message": "The server had an error"}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    chunks = []
    with pytest.raises(ProviderError) as excinfo:
        async for chunk in make_provider(handler).stream(MESSAGES):
            chunks.append(chunk)
    assert "stream error event" in str(excinfo.value)
    # The failure must not have been delivered as a successful final chunk.
    assert not any(chunk.done for chunk in chunks)


async def test_openai_compat_stream_yields_tool_call_deltas_and_finish_reason():
    # Regression: delta.tool_calls was previously dropped entirely (only
    # delta.content was read) — a forced tool_choice call (whose only content
    # is this) yielded zero content chunks. finish_reason was also never
    # captured for streaming at all, unlike complete().
    def handler(request):
        body = (
            b'data: {"id": "chatcmpl_1", "model": "m", "choices": [{"index": 0, '
            b'"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", '
            b'"function": {"name": "answer", "arguments": ""}}]}, "finish_reason": null}]}\n\n'
            b'data: {"id": "chatcmpl_1", "model": "m", "choices": [{"index": 0, '
            b'"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\\"a"}}]}, '
            b'"finish_reason": null}]}\n\n'
            b'data: {"id": "chatcmpl_1", "model": "m", "choices": [{"index": 0, '
            b'"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "nswer\\": 42}"}}]}, '
            b'"finish_reason": null}]}\n\n'
            b'data: {"id": "chatcmpl_1", "model": "m", "choices": [{"index": 0, '
            b'"delta": {}, "finish_reason": "tool_calls"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    chunks = [chunk async for chunk in make_provider(handler).stream(MESSAGES)]
    tool_calls = [c.tool_call for c in chunks if c.tool_call is not None]
    assert len(tool_calls) == 3
    assert tool_calls[0].id == "call_1"
    assert tool_calls[0].name == "answer"
    assert "".join(tc.partial_json for tc in tool_calls) == '{"answer": 42}'

    final = chunks[-1]
    assert final.done is True
    assert final.finish_reason == "tool_calls"
    assert final.raw["id"] == "chatcmpl_1"
    message = final.raw["choices"][0]["message"]
    assert message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "answer", "arguments": '{"answer": 42}'},
        }
    ]


async def test_anthropic_stream_error_event_raises():
    from byoai.providers.anthropic import AnthropicProvider

    def handler(request):
        body = (
            b'data: {"type": "message_start",'
            b' "message": {"model": "c", "usage": {"input_tokens": 1}}}\n\n'
            b'data: {"type": "error",'
            b' "error": {"type": "overloaded_error", "message": "Overloaded"}}\n\n'
        )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    client = httpx.AsyncClient(
        base_url="https://mock.test", transport=httpx.MockTransport(handler)
    )
    provider = AnthropicProvider(model="c", client=client)
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream(MESSAGES):
            pass
    assert "overloaded_error" in str(excinfo.value)
    assert excinfo.value.retryable is True  # overloaded → router may retry/fall back


async def test_anthropic_requires_api_key(monkeypatch):
    from byoai.errors import ConfigurationError
    from byoai.providers.anthropic import AnthropicProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        AnthropicProvider(model="c")


async def test_anthropic_allows_keyless_gateway_via_default_headers(monkeypatch):
    from byoai.providers.anthropic import AnthropicProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # A gateway that authenticates out-of-band (mTLS, network policy) or under
    # its own header must still be constructable without an Anthropic key.
    provider = AnthropicProvider(
        model="c", default_headers={"Authorization": "Bearer gateway-token"}
    )
    assert "x-api-key" not in provider._client.headers
    await provider.close()


async def test_anthropic_unrelated_default_header_does_not_bypass_key_check(monkeypatch):
    from byoai.errors import ConfigurationError
    from byoai.providers.anthropic import AnthropicProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # An unrelated header (e.g. tracing) carries no auth intent — it must not
    # silently disable the fail-fast the way a real auth header does above.
    with pytest.raises(ConfigurationError):
        AnthropicProvider(model="c", default_headers={"X-Trace-Id": "abc"})


async def test_gemini_unrelated_default_header_does_not_bypass_key_check(monkeypatch):
    from byoai.errors import ConfigurationError
    from byoai.providers.gemini import GeminiProvider

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        GeminiProvider(model="c", default_headers={"X-Trace-Id": "abc"})


async def test_gemini_rejects_list_valued_content_with_clear_error():
    # Message.content also accepts Anthropic-style content blocks; Gemini's
    # payload builder only understands plain text — mixing a block-content
    # message into a Gemini-bound request must fail cleanly, not with a
    # cryptic TypeError from str.join()/dict construction. ProviderError (not
    # ConfigurationError): this is a per-request provider incompatibility, so
    # ProviderRouter's fallback chain can catch it and try the next provider
    # instead of the whole request hard-failing.
    from byoai.providers.gemini import GeminiProvider

    def handler(request):
        raise AssertionError("should have raised before making a request")

    client = httpx.AsyncClient(
        base_url="https://mock.test", transport=httpx.MockTransport(handler)
    )
    provider = GeminiProvider(model="c", api_key="k", client=client)
    blocky = [Message(role="user", content=[{"type": "text", "text": "hi"}])]
    with pytest.raises(ProviderError, match="content blocks") as excinfo:
        await provider.complete(blocky)
    assert excinfo.value.retryable is False


async def test_gemini_drops_metadata_but_still_forwards_other_options():
    # Regression: _payload used to blindly `payload.update(options)`, which
    # forwarded a metadata= key (Runtime.execute()'s provider_metadata=,
    # Anthropic's own top-level field) straight into Gemini's generateContent
    # body — a field Gemini's real API doesn't recognize and would reject.
    # options.pop("metadata", None) now drops it before the merge, while
    # other real Gemini options (e.g. temperature) still get forwarded.
    import json as stdlib_json

    from byoai.providers.gemini import GeminiProvider

    captured = {}

    def handler(request):
        captured["body"] = stdlib_json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            },
        )

    client = httpx.AsyncClient(
        base_url="https://mock.test", transport=httpx.MockTransport(handler)
    )
    provider = GeminiProvider(model="c", api_key="k", client=client)
    await provider.complete(
        MESSAGES, metadata={"user_id": "u1"}, temperature=0.5
    )
    assert "metadata" not in captured["body"]
    assert captured["body"]["generationConfig"]["temperature"] == 0.5


async def test_gemini_stream_final_chunk_carries_finish_reason():
    # Regression: stream()'s final StreamChunk never set finish_reason at all
    # (unlike complete(), which reads candidates[0].finishReason) — a
    # Gemini-streamed tool_use/max_tokens turn couldn't be distinguished from
    # a normal completion by any caller relying on the field.
    from byoai.providers.gemini import GeminiProvider

    def handler(request):
        body = (
            b'data: {"candidates": [{"content": {"parts": [{"text": "hi"}]}, '
            b'"finishReason": "STOP"}], '
            b'"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}\n\n'
        )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    client = httpx.AsyncClient(
        base_url="https://mock.test", transport=httpx.MockTransport(handler)
    )
    provider = GeminiProvider(model="c", api_key="k", client=client)
    chunks = [chunk async for chunk in provider.stream(MESSAGES)]
    assert chunks[-1].done is True
    assert chunks[-1].finish_reason == "stop"


async def test_openai_compat_rejects_list_valued_content_with_clear_error():
    # Regression: Message.to_dict() used to pass Anthropic-shaped content
    # blocks (e.g. {"type": "tool_use", ...}) straight through into the
    # OpenAI-format `content` field — garbage, since those aren't valid
    # OpenAI content-part shapes. _payload now calls require_text_content()
    # on every message first and raises ProviderError instead — not
    # ConfigurationError, so ProviderRouter's fallback chain can catch it and
    # try the next provider instead of hard-failing the whole request.
    def handler(request):
        raise AssertionError("should have raised before making a request")

    blocky = [
        Message(
            role="user",
            content=[{"type": "tool_use", "id": "x", "name": "y", "input": {}}],
        )
    ]
    with pytest.raises(ProviderError, match="content blocks") as excinfo:
        await make_provider(handler).complete(blocky)
    assert excinfo.value.retryable is False


# -- system-field building / cache_system / cache-token parsing -------------


def make_anthropic_provider(handler, **kwargs):
    from byoai.providers.anthropic import AnthropicProvider

    client = httpx.AsyncClient(
        base_url="https://mock.test", transport=httpx.MockTransport(handler)
    )
    return AnthropicProvider(model="c", client=client, **kwargs)


def anthropic_ok_response(request):
    return httpx.Response(
        200,
        json={
            "model": "c",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        },
    )


async def test_anthropic_single_system_message_plain_string_by_default():
    import json as stdlib_json

    captured = {}

    def handler(request):
        captured["body"] = stdlib_json.loads(request.read())
        return anthropic_ok_response(request)

    messages = [Message(role="system", content="be terse"), Message(role="user", content="hi")]
    provider = make_anthropic_provider(handler)
    await provider.complete(messages)
    assert captured["body"]["system"] == "be terse"


async def test_anthropic_single_system_message_cache_system_wraps_in_block():
    import json as stdlib_json

    captured = {}

    def handler(request):
        captured["body"] = stdlib_json.loads(request.read())
        return anthropic_ok_response(request)

    messages = [Message(role="system", content="be terse"), Message(role="user", content="hi")]
    provider = make_anthropic_provider(handler, cache_system=True)
    await provider.complete(messages)
    assert captured["body"]["system"] == [
        {"type": "text", "text": "be terse", "cache_control": {"type": "ephemeral"}}
    ]


async def test_anthropic_multiple_system_messages_joined_with_blank_line():
    import json as stdlib_json

    captured = {}

    def handler(request):
        captured["body"] = stdlib_json.loads(request.read())
        return anthropic_ok_response(request)

    messages = [
        Message(role="system", content="first"),
        Message(role="system", content="second"),
        Message(role="user", content="hi"),
    ]
    provider = make_anthropic_provider(handler)
    await provider.complete(messages)
    assert captured["body"]["system"] == "first\n\nsecond"


async def test_anthropic_list_content_system_message_passed_through():
    import json as stdlib_json

    captured = {}

    def handler(request):
        captured["body"] = stdlib_json.loads(request.read())
        return anthropic_ok_response(request)

    blocks = [{"type": "text", "text": "pre-built", "cache_control": {"type": "ephemeral"}}]
    messages = [Message(role="system", content=blocks), Message(role="user", content="hi")]
    provider = make_anthropic_provider(handler)
    await provider.complete(messages)
    assert captured["body"]["system"] == blocks


async def test_anthropic_mixed_str_and_list_system_messages_raises_configuration_error():
    from byoai.errors import ConfigurationError
    from byoai.providers.base import build_anthropic_system_field

    messages = [
        Message(role="system", content="plain"),
        Message(role="system", content=[{"type": "text", "text": "blocky"}]),
    ]
    with pytest.raises(ConfigurationError):
        build_anthropic_system_field(messages, cache_system=False)


async def test_anthropic_empty_block_list_system_message_omits_field_entirely():
    # Regression: a system message whose content was an empty list produced
    # payload["system"] = [] instead of omitting the field — Anthropic's API
    # rejects an empty system array with a 400 instead of treating it the
    # same as no system prompt at all.
    from byoai.providers.base import build_anthropic_system_field

    messages = [Message(role="system", content=[]), Message(role="user", content="hi")]
    assert build_anthropic_system_field(messages, cache_system=False) is None


async def test_anthropic_response_cache_tokens_parsed_into_usage():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "c",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 3,
                },
                "stop_reason": "end_turn",
            },
        )

    provider = make_anthropic_provider(handler)
    response = await provider.complete(MESSAGES)
    assert response.usage.cache_read_tokens == 7
    assert response.usage.cache_creation_tokens == 3
    # total_tokens is deliberately unaffected by cache_* fields.
    assert response.usage.total_tokens == 15


async def test_anthropic_stream_parses_cache_tokens_and_finish_reason():
    # Regression: stream()'s final StreamChunk never had cache_read_tokens/
    # cache_creation_tokens/finish_reason populated (all silently 0/None)
    # even when the SSE events carried that data, unlike complete() which did
    # parse them. stream() now reads cache_read_input_tokens/
    # cache_creation_input_tokens from message_start's usage dict, and
    # stop_reason from message_delta's delta dict.
    def handler(request):
        body = (
            b'data: {"type": "message_start", "message": {"model": "c", '
            b'"usage": {"input_tokens": 10, "cache_read_input_tokens": 7, '
            b'"cache_creation_input_tokens": 3}}}\n\n'
            b'data: {"type": "content_block_delta", '
            b'"delta": {"type": "text_delta", "text": "hi"}}\n\n'
            b'data: {"type": "message_delta", '
            b'"delta": {"stop_reason": "tool_use"}, '
            b'"usage": {"output_tokens": 5}}\n\n'
            b'data: {"type": "message_stop"}\n\n'
        )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    provider = make_anthropic_provider(handler)
    chunks = [chunk async for chunk in provider.stream(MESSAGES)]
    final = chunks[-1]
    assert final.done is True
    assert final.usage is not None
    assert final.usage.cache_read_tokens == 7
    assert final.usage.cache_creation_tokens == 3
    assert final.finish_reason == "tool_use"


async def test_anthropic_stream_yields_tool_call_deltas_instead_of_dropping_them():
    # Regression: a forced tool_choice turn's only content is
    # content_block_start(tool_use) + input_json_delta events — previously
    # unhandled, so stream() yielded zero content chunks for a tool-only turn.
    def handler(request):
        body = (
            b'data: {"type": "message_start", "message": {"id": "msg_1", '
            b'"model": "c", "usage": {"input_tokens": 10}}}\n\n'
            b'data: {"type": "content_block_start", "index": 0, '
            b'"content_block": {"type": "tool_use", "id": "toolu_1", "name": "answer"}}\n\n'
            b'data: {"type": "content_block_delta", "index": 0, '
            b'"delta": {"type": "input_json_delta", "partial_json": "{\\"a"}}\n\n'
            b'data: {"type": "content_block_delta", "index": 0, '
            b'"delta": {"type": "input_json_delta", "partial_json": "nswer\\": 42}"}}\n\n'
            b'data: {"type": "message_delta", '
            b'"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}\n\n'
            b'data: {"type": "message_stop"}\n\n'
        )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    provider = make_anthropic_provider(handler)
    chunks = [chunk async for chunk in provider.stream(MESSAGES)]

    tool_calls = [c.tool_call for c in chunks if c.tool_call is not None]
    assert len(tool_calls) == 3  # block-start + two partial_json fragments
    start = tool_calls[0]
    assert start.id == "toolu_1"
    assert start.name == "answer"
    assert start.partial_json == ""
    fragments = "".join(tc.partial_json for tc in tool_calls[1:])
    assert fragments == '{"answer": 42}'

    final = chunks[-1]
    assert final.done is True
    assert final.raw["id"] == "msg_1"
    assert final.raw["stop_reason"] == "tool_use"
    assert final.raw["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": "answer", "input": {"answer": 42}}
    ]


async def test_anthropic_stream_malformed_tool_use_json_raises_instead_of_input_none():
    # Regression: a tool_use block whose accumulated partial_json fails to
    # parse (truncated stream, provider-side bug) used to silently become
    # input=None in raw["content"] instead of raising — a caller feeding raw
    # straight into a tool executor expects input to always be a dict and
    # would crash with a confusing AttributeError/TypeError instead.
    def handler(request):
        body = (
            b'data: {"type": "message_start", "message": {"id": "msg_1", '
            b'"model": "c", "usage": {"input_tokens": 10}}}\n\n'
            b'data: {"type": "content_block_start", "index": 0, '
            b'"content_block": {"type": "tool_use", "id": "toolu_1", "name": "answer"}}\n\n'
            b'data: {"type": "content_block_delta", "index": 0, '
            b'"delta": {"type": "input_json_delta", "partial_json": "{not valid json"}}\n\n'
            b'data: {"type": "message_delta", '
            b'"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}\n\n'
            b'data: {"type": "message_stop"}\n\n'
        )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    provider = make_anthropic_provider(handler)
    with pytest.raises(ProviderError, match="malformed tool_use input JSON"):
        async for _ in provider.stream(MESSAGES):
            pass


async def test_anthropic_stream_reconstructs_thinking_block_in_raw():
    # Regression: raw["content"]'s reconstruction only merged text_delta/
    # input_json_delta fragments — a thinking block's thinking_delta/
    # signature_delta events matched neither branch and were silently
    # dropped, so an extended-thinking turn's raw["content"] carried a
    # "thinking" block with empty thinking/signature strings instead of the
    # model's actual reasoning trace (which Anthropic requires sent back
    # unmodified on the next turn when thinking + tool_use are interleaved).
    def handler(request):
        body = (
            b'data: {"type": "message_start", "message": {"id": "msg_1", '
            b'"model": "c", "usage": {"input_tokens": 10}}}\n\n'
            b'data: {"type": "content_block_start", "index": 0, '
            b'"content_block": {"type": "thinking", "thinking": "", "signature": ""}}\n\n'
            b'data: {"type": "content_block_delta", "index": 0, '
            b'"delta": {"type": "thinking_delta", "thinking": "Let me con"}}\n\n'
            b'data: {"type": "content_block_delta", "index": 0, '
            b'"delta": {"type": "thinking_delta", "thinking": "sider this."}}\n\n'
            b'data: {"type": "content_block_delta", "index": 0, '
            b'"delta": {"type": "signature_delta", "signature": "sig123"}}\n\n'
            b'data: {"type": "message_delta", '
            b'"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}\n\n'
            b'data: {"type": "message_stop"}\n\n'
        )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    provider = make_anthropic_provider(handler)
    chunks = [chunk async for chunk in provider.stream(MESSAGES)]

    # Not surfaced as visible answer text — thinking content stays internal
    # to raw, never yielded via StreamChunk.delta.
    assert "".join(c.delta for c in chunks if not c.done) == ""
    final = chunks[-1]
    assert final.raw["content"] == [
        {"type": "thinking", "thinking": "Let me consider this.", "signature": "sig123"}
    ]

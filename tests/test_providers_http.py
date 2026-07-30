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
    # cryptic TypeError from str.join()/dict construction.
    from byoai.errors import ConfigurationError
    from byoai.providers.gemini import GeminiProvider

    def handler(request):
        raise AssertionError("should have raised before making a request")

    client = httpx.AsyncClient(
        base_url="https://mock.test", transport=httpx.MockTransport(handler)
    )
    provider = GeminiProvider(model="c", api_key="k", client=client)
    blocky = [Message(role="user", content=[{"type": "text", "text": "hi"}])]
    with pytest.raises(ConfigurationError, match="content blocks"):
        await provider.complete(blocky)


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


async def test_openai_compat_rejects_list_valued_content_with_clear_error():
    # Regression: Message.to_dict() used to pass Anthropic-shaped content
    # blocks (e.g. {"type": "tool_use", ...}) straight through into the
    # OpenAI-format `content` field — garbage, since those aren't valid
    # OpenAI content-part shapes. _payload now calls require_text_content()
    # on every message first and raises ConfigurationError instead.
    from byoai.errors import ConfigurationError

    def handler(request):
        raise AssertionError("should have raised before making a request")

    blocky = [
        Message(
            role="user",
            content=[{"type": "tool_use", "id": "x", "name": "y", "input": {}}],
        )
    ]
    with pytest.raises(ConfigurationError, match="content blocks"):
        await make_provider(handler).complete(blocky)


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

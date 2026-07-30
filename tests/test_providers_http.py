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

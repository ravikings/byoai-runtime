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


async def test_http_date_retry_after_does_not_crash():
    def handler(request):
        return httpx.Response(
            429,
            json={"error": {"message": "slow down"}},
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
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

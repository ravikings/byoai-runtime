"""AnthropicBedrockProvider / AnthropicVertexProvider — against a fake SDK
client (client= bypasses AsyncAnthropicBedrock/AsyncAnthropicVertex
construction entirely, so no real AWS/GCP credentials are needed). Verifies
message translation, error classification (reuses raise_for_status on the
SDK's own httpx.Response, same as every other adapter), and streaming.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

anthropic = pytest.importorskip("anthropic")

from byoai.errors import ConfigurationError, ProviderError, RateLimitError  # noqa: E402
from byoai.providers.anthropic_cloud import (  # noqa: E402
    AnthropicBedrockProvider,
    AnthropicVertexProvider,
)
from byoai.types import Message  # noqa: E402

MESSAGES = [Message(role="system", content="be terse"), Message(role="user", content="hi")]


def make_response(text="hi there", model="claude-x", stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model=model,
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        stop_reason=stop_reason,
    )


def make_status_error(status_code: int, *, retry_after: str | None = None) -> Exception:
    headers = {"retry-after": retry_after} if retry_after else {}
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://mock.test"),
        headers=headers,
        json={"error": {"message": "boom"}},
    )
    cls = anthropic.RateLimitError if status_code == 429 else anthropic.APIStatusError
    return cls("boom", response=response, body={"error": {"message": "boom"}})


class FakeMessagesAPI:
    def __init__(self, *, response=None, raise_exc=None, stream_events=(), stream_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self._stream_events = list(stream_events)
        self._stream_exc = stream_exc
        self.last_kwargs: dict = {}

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._raise_exc:
            raise self._raise_exc
        return self._response

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeStreamCM(self._stream_events, self._stream_exc, self._response)


class _FakeStream:
    def __init__(self, events, exc, final_message):
        self._exc = exc
        self._final_message = final_message
        self.text_stream = self._gen()
        self._events = events

    async def _gen(self):
        for delta in self._events:
            yield delta
        if self._exc:
            raise self._exc

    async def get_final_message(self):
        return self._final_message


class _FakeStreamCM:
    def __init__(self, events, exc, final_message):
        self._stream = _FakeStream(events, exc, final_message)

    async def __aenter__(self):
        return self._stream

    async def __aexit__(self, *exc_info):
        return False


class FakeClient:
    def __init__(self, messages_api: FakeMessagesAPI):
        self.messages = messages_api
        self.closed = False

    async def close(self):
        self.closed = True


async def test_bedrock_complete_translates_response():
    api = FakeMessagesAPI(response=make_response(text="answer"))
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    result = await provider.complete(MESSAGES)
    assert result.content == "answer"
    assert result.provider == "bedrock"
    assert result.usage.total_tokens == 8
    assert api.last_kwargs["system"] == "be terse"
    assert api.last_kwargs["messages"] == [{"role": "user", "content": "hi"}]


async def test_bedrock_rate_limit_reuses_shared_classification():
    api = FakeMessagesAPI(raise_exc=make_status_error(429, retry_after="7"))
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    with pytest.raises(RateLimitError) as excinfo:
        await provider.complete(MESSAGES)
    assert excinfo.value.retry_after == 7.0


async def test_bedrock_overloaded_529_is_retryable():
    api = FakeMessagesAPI(raise_exc=make_status_error(529))
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    with pytest.raises(ProviderError) as excinfo:
        await provider.complete(MESSAGES)
    assert excinfo.value.retryable is True


async def test_bedrock_connection_error_wrapped_retryable():
    api = FakeMessagesAPI(raise_exc=anthropic.APIConnectionError(request=httpx.Request("POST", "https://mock.test")))
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    with pytest.raises(ProviderError) as excinfo:
        await provider.complete(MESSAGES)
    assert excinfo.value.retryable is True


async def test_bedrock_stream_yields_deltas_then_done_with_usage():
    api = FakeMessagesAPI(stream_events=["hel", "lo"], response=make_response(model="claude-x"))
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    chunks = [c async for c in provider.stream(MESSAGES)]
    assert [c.delta for c in chunks if not c.done] == ["hel", "lo"]
    assert chunks[-1].done is True
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 8


async def test_bedrock_stream_error_wrapped():
    api = FakeMessagesAPI(stream_exc=make_status_error(500))
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    with pytest.raises(ProviderError):
        async for _ in provider.stream(MESSAGES):
            pass


async def test_bedrock_close_does_not_close_a_caller_supplied_client():
    # Regression: close() used to unconditionally close self._client, so a
    # caller sharing a client= across multiple provider instances (or managing
    # its lifecycle themselves) had it torn out from under them by close().
    api = FakeMessagesAPI(response=make_response())
    client = FakeClient(api)
    provider = AnthropicBedrockProvider(model="m", client=client)
    await provider.close()
    assert client.closed is False


async def test_bedrock_close_closes_a_client_it_built_itself(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    provider = AnthropicBedrockProvider(model="m")
    closed = {"v": False}

    async def fake_close():
        closed["v"] = True

    provider._client.close = fake_close
    await provider.close()
    assert closed["v"] is True


async def test_bedrock_requires_region_when_no_client_and_no_env(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(ConfigurationError, match="aws_region"):
        AnthropicBedrockProvider(model="m")


async def test_bedrock_region_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    provider = AnthropicBedrockProvider(model="m")
    assert provider.name == "bedrock"
    await provider.close()


async def test_vertex_complete_translates_response():
    api = FakeMessagesAPI(response=make_response(text="vertex answer"))
    provider = AnthropicVertexProvider(model="m", client=FakeClient(api))
    result = await provider.complete(MESSAGES)
    assert result.content == "vertex answer"
    assert result.provider == "vertex"


async def test_vertex_requires_project_and_region(monkeypatch):
    env_vars = (
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "GOOGLE_CLOUD_PROJECT",
        "ANTHROPIC_VERTEX_REGION",
        "CLOUD_ML_REGION",
    )
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigurationError, match="project_id and region"):
        AnthropicVertexProvider(model="m")


async def test_vertex_project_and_region_fall_back_to_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
    provider = AnthropicVertexProvider(model="m")
    assert provider.name == "vertex"
    await provider.close()


async def test_declarative_bedrock_via_runtime():
    from byoai import Runtime

    runtime = Runtime(llm={"provider": "bedrock", "model": "m", "aws_region": "us-east-1"})
    assert runtime.router is not None
    assert runtime.router.providers[0].name == "bedrock"
    await runtime.close()


async def test_declarative_vertex_via_runtime():
    from byoai import Runtime

    runtime = Runtime(
        llm={"provider": "vertex", "model": "m", "project_id": "p", "region": "us-east5"}
    )
    assert runtime.router is not None
    assert runtime.router.providers[0].name == "vertex"
    await runtime.close()


# -- cache_system / build_anthropic_system_field / cache-token parsing ------


async def test_bedrock_cache_system_wraps_system_field_in_block():
    api = FakeMessagesAPI(response=make_response())
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api), cache_system=True)
    await provider.complete(MESSAGES)
    assert api.last_kwargs["system"] == [
        {"type": "text", "text": "be terse", "cache_control": {"type": "ephemeral"}}
    ]


async def test_bedrock_list_content_system_message_passed_through():
    api = FakeMessagesAPI(response=make_response())
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    blocks = [{"type": "text", "text": "pre-built"}]
    messages = [Message(role="system", content=blocks), Message(role="user", content="hi")]
    await provider.complete(messages)
    assert api.last_kwargs["system"] == blocks


async def test_bedrock_mixed_system_content_raises_configuration_error():
    api = FakeMessagesAPI(response=make_response())
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    messages = [
        Message(role="system", content="plain"),
        Message(role="system", content=[{"type": "text", "text": "blocky"}]),
    ]
    with pytest.raises(ConfigurationError):
        await provider.complete(messages)


async def test_bedrock_complete_parses_cache_tokens_when_present():
    response = make_response()
    response.usage.cache_read_input_tokens = 7
    response.usage.cache_creation_input_tokens = 3
    api = FakeMessagesAPI(response=response)
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    result = await provider.complete(MESSAGES)
    assert result.usage.cache_read_tokens == 7
    assert result.usage.cache_creation_tokens == 3


async def test_bedrock_complete_defaults_cache_tokens_to_zero_when_absent():
    # make_response()'s usage SimpleNamespace has no cache_read_input_tokens/
    # cache_creation_input_tokens attributes at all (older SDK / fake double) —
    # the getattr(..., 0) or 0 fallback must not raise and must default to 0.
    api = FakeMessagesAPI(response=make_response())
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    result = await provider.complete(MESSAGES)
    assert result.usage.cache_read_tokens == 0
    assert result.usage.cache_creation_tokens == 0


async def test_bedrock_stream_final_chunk_carries_cache_tokens_and_finish_reason():
    # Regression: stream()'s final StreamChunk didn't surface cache_read_tokens/
    # cache_creation_tokens/finish_reason from the SDK's final message even
    # though complete() parsed the equivalent fields.
    response = make_response(stop_reason="tool_use")
    response.usage.cache_read_input_tokens = 7
    response.usage.cache_creation_input_tokens = 3
    api = FakeMessagesAPI(stream_events=["hel", "lo"], response=response)
    provider = AnthropicBedrockProvider(model="m", client=FakeClient(api))
    chunks = [c async for c in provider.stream(MESSAGES)]
    final = chunks[-1]
    assert final.done is True
    assert final.usage is not None
    assert final.usage.cache_read_tokens == 7
    assert final.usage.cache_creation_tokens == 3
    assert final.finish_reason == "tool_use"

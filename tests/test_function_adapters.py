"""Bare-callable adapters: FunctionProvider and FunctionVectorStore.

Mirrors the existing FunctionStage (pipeline stages) and embedder= callable
patterns — a plain async function should work for any protocol that has one
natural operation, without requiring a class.
"""
from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from byoai import Runtime
from byoai.errors import AllProvidersFailed, ProviderError
from byoai.providers.base import FunctionProvider
from byoai.providers.router import ProviderRouter
from byoai.types import Document, Message, ProviderResponse, StreamChunk, Usage
from byoai.vector.base import FunctionVectorStore

MESSAGES = [Message(role="user", content="hi")]


async def test_bare_provider_function_returns_str():
    async def my_gateway(messages: list[Message], **options) -> str:
        return "hello from gateway"

    router = ProviderRouter([my_gateway])
    response = await router.complete(MESSAGES)
    assert response.content == "hello from gateway"
    assert response.provider == "my_gateway"  # name defaults to __name__


async def test_bare_provider_function_returns_provider_response():
    async def my_gateway(messages: list[Message], **options) -> ProviderResponse:
        return ProviderResponse(
            content="rich reply",
            model="claude-x",
            provider="ignored",  # FunctionProvider does not override an explicit response
            usage=Usage(input_tokens=3, output_tokens=5),
        )

    router = ProviderRouter([my_gateway])
    response = await router.complete(MESSAGES)
    assert response.content == "rich reply"
    assert response.usage.total_tokens == 8


async def test_bare_provider_function_forwards_options():
    seen = {}

    async def my_gateway(messages: list[Message], **options) -> str:
        seen.update(options)
        return "ok"

    runtime = Runtime(providers=[my_gateway])
    await runtime.execute("hi", tenant="acme-corp")
    await runtime.close()
    assert seen.get("tenant") == "acme-corp"


async def test_function_provider_custom_name_and_model():
    async def fn(messages, **options):
        return "x"

    provider = FunctionProvider(fn, name="custom", model="my-model")
    response = await provider.complete(MESSAGES)
    assert response.provider == "custom"
    assert response.model == "my-model"


async def test_function_provider_stream_without_stream_fn_raises_provider_error():
    # A ProviderError (not a bare NotImplementedError) so ProviderRouter.stream()'s
    # `except ProviderError` can fall back to another provider instead of crashing.
    async def fn(messages, **options):
        return "x"

    provider = FunctionProvider(fn)
    with pytest.raises(ProviderError, match="stream_fn"):
        async for _ in provider.stream(MESSAGES):
            pass


async def test_bare_provider_function_raising_plain_exception_is_wrapped_and_falls_back():
    # Regression: a non-ProviderError exception from the wrapped function used to
    # propagate raw, past ProviderRouter's `except ProviderError`, crashing the
    # request instead of falling back — the same bug class the chaos-testing pass
    # found and fixed for SemanticCacheLookup.
    async def flaky_gateway(messages, **options):
        raise ConnectionError("dns lookup failed")

    router = ProviderRouter([flaky_gateway, FakeProvider(reply="fallback worked")])
    response = await router.complete(MESSAGES)
    assert response.content == "fallback worked"


async def test_bare_provider_function_all_fail_raises_all_providers_failed():
    async def flaky_gateway(messages, **options):
        raise ConnectionError("dns lookup failed")

    router = ProviderRouter([flaky_gateway])
    with pytest.raises(AllProvidersFailed) as excinfo:
        await router.complete(MESSAGES)
    assert all(isinstance(e, ProviderError) for e in excinfo.value.errors)


async def test_function_provider_stream_fn_raising_plain_exception_is_wrapped():
    async def fn(messages, **options):
        return "unused"

    async def flaky_stream(messages, **options):
        raise TimeoutError("upstream timed out")
        yield  # pragma: no cover - unreachable, makes this a generator

    provider = FunctionProvider(fn, stream_fn=flaky_stream)
    with pytest.raises(ProviderError, match="upstream timed out"):
        async for _ in provider.stream(MESSAGES):
            pass


async def test_function_provider_stream_fn_plain_strings_gets_trailing_done():
    async def fn(messages, **options):
        return "unused"

    async def stream_fn(messages, **options):
        yield "hel"
        yield "lo"

    provider = FunctionProvider(fn, stream_fn=stream_fn)
    chunks = [c async for c in provider.stream(MESSAGES)]
    assert [c.delta for c in chunks if not c.done] == ["hel", "lo"]
    assert chunks[-1].done is True


async def test_function_provider_stream_fn_full_chunks_no_duplicate_done():
    async def fn(messages, **options):
        return "unused"

    async def stream_fn(messages, **options):
        yield StreamChunk(delta="hi ")
        yield StreamChunk(done=True, usage=Usage(input_tokens=1, output_tokens=1))

    provider = FunctionProvider(fn, stream_fn=stream_fn)
    chunks = [c async for c in provider.stream(MESSAGES)]
    assert sum(1 for c in chunks if c.done) == 1
    assert chunks[-1].usage.total_tokens == 2


async def test_runtime_bare_provider_end_to_end_streaming():
    async def fn(messages, **options):
        return "unused"

    async def stream_fn(messages, **options):
        for word in ["a ", "b "]:
            yield word

    runtime = Runtime(providers=[FunctionProvider(fn, stream_fn=stream_fn)])
    chunks = [c async for c in runtime.stream("hi")]
    await runtime.close()
    text = "".join(c.delta for c in chunks if not c.done)
    assert text == "a b "


async def test_bare_vector_store_function():
    async def my_search(embedding, *, top_k, filters=None) -> list[Document]:
        return [Document(id="1", content="found it", metadata={"top_k": top_k})]

    runtime = Runtime(providers=[FunctionProvider(_dummy_provider)], vector_store=my_search)
    docs = await runtime.vector_store.search([0.1, 0.2], top_k=3)
    await runtime.close()
    assert docs[0].content == "found it"
    assert docs[0].metadata["top_k"] == 3


async def test_function_vector_store_direct():
    async def my_search(embedding, *, top_k, filters=None) -> list[Document]:
        return []

    store = FunctionVectorStore(my_search, name="custom-store")
    assert store.name == "custom-store"
    assert await store.search([0.1], top_k=1) == []
    await store.close()  # must not raise


async def _dummy_provider(messages, **options):
    return "unused"

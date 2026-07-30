"""Bare-callable adapters: FunctionProvider and FunctionVectorStore.

Mirrors the existing FunctionStage (pipeline stages) and embedder= callable
patterns — a plain async function should work for any protocol that has one
natural operation, without requiring a class.
"""
from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from byoai import Runtime
from byoai.errors import AllProvidersFailed, ProviderError, VectorStoreError
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


async def test_bare_provider_function_exception_defaults_non_retryable():
    # Unlike a transport error (httpx.HTTPError), we don't know the cause of an
    # arbitrary exception from a wrapped function — most causes (a bug, bad
    # credentials baked into the closure) aren't transient, so retrying by
    # default would just add pointless backoff sleep on every failing call.
    calls = {"n": 0}

    async def flaky_gateway(messages, **options):
        calls["n"] += 1
        raise ConnectionError("dns lookup failed")

    router = ProviderRouter([flaky_gateway])
    with pytest.raises(AllProvidersFailed) as excinfo:
        await router.complete(MESSAGES)
    assert excinfo.value.errors[0].retryable is False
    assert calls["n"] == 1  # no retries attempted


async def test_bare_provider_function_returning_none_raises_instead_of_stringifying():
    # Regression: a buggy wrapped function that falls through without an
    # explicit `return` yields None, which used to become the literal text
    # "None" delivered to the caller as if it were a real answer.
    async def buggy_gateway(messages, **options):
        if False:  # pragma: no cover - unreachable, simulates a missed branch
            return "unreachable"

    provider = FunctionProvider(buggy_gateway)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ProviderError, match="NoneType"):
        await provider.complete(MESSAGES)


async def test_function_provider_stream_fn_yielding_none_raises_instead_of_stringifying():
    async def fn(messages, **options):
        return "unused"

    async def buggy_stream(messages, **options):
        yield "ok"
        yield None

    provider = FunctionProvider(fn, stream_fn=buggy_stream)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ProviderError, match="NoneType"):
        async for _ in provider.stream(MESSAGES):
            pass


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
    assert chunks[-1].usage is not None
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
    assert runtime.vector_store is not None
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


async def test_function_vector_store_wraps_raw_exception():
    # Regression: a raw exception from the wrapped search function used to
    # propagate as its own type instead of a catchable VectorStoreError.
    async def flaky_search(embedding, *, top_k, filters=None):
        raise ConnectionError("index unreachable")

    store = FunctionVectorStore(flaky_search)
    with pytest.raises(VectorStoreError, match="index unreachable"):
        await store.search([0.1], top_k=1)


async def test_function_vector_store_returning_none_raises_instead_of_crashing_downstream():
    # Regression: a buggy search function returning None (e.g. a missed
    # `return`) used to propagate as `ctx.documents = None`, crashing any
    # downstream code (e.g. `len(documents)`) that assumes a list.
    async def buggy_search(embedding, *, top_k, filters=None):
        if False:  # pragma: no cover - unreachable, simulates a missed branch
            return []

    store = FunctionVectorStore(buggy_search)  # pyright: ignore[reportArgumentType]
    with pytest.raises(VectorStoreError, match="NoneType"):
        await store.search([0.1], top_k=1)


async def _dummy_provider(messages, **options):
    return "unused"

"""Regression tests for the configurability audit: knobs that must actually
change behavior, not just exist as accepted-but-ignored kwargs."""

from __future__ import annotations

import httpx
import pytest
from tests.conftest import FakeProvider

from byoai import Runtime
from byoai.cache.memory import MemoryCache
from byoai.context import RequestContext
from byoai.providers.anthropic import AnthropicProvider
from byoai.providers.embeddings import OpenAICompatEmbedder
from byoai.providers.gemini import GeminiProvider
from byoai.providers.openai_compat import OpenAICompatProvider
from byoai.stages import CacheLookup, ContextResolver
from byoai.vector.pinecone import PineconeVectorStore
from byoai.vector.qdrant import QdrantVectorStore
from byoai.workers import Job, MemoryJobQueue, RuntimeWorker

# --- provider path/header/retry overrides -----------------------------------


async def test_openai_compat_chat_path_override():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })

    client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler))
    provider = OpenAICompatProvider(
        model="m", api_key="k", client=client, chat_path="/v2/gateway/completions"
    )
    await provider.complete([])
    assert seen["path"] == "/v2/gateway/completions"


async def test_openai_compat_retryable_status_override():
    def handler(request):
        return httpx.Response(418, text="teapot")

    client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler))
    provider = OpenAICompatProvider(
        model="m", api_key="k", client=client, retryable_status={418}
    )
    from byoai.errors import ProviderError

    with pytest.raises(ProviderError) as excinfo:
        await provider.complete([])
    assert excinfo.value.retryable is True  # 418 not in defaults, but is in override


async def test_anthropic_api_version_and_default_headers():
    # default_headers/api_version only apply when the provider builds its own
    # client (client= bypasses them, same contract as OpenAICompatProvider) —
    # inspect the constructed client's headers directly.
    provider = AnthropicProvider(
        model="m", api_key="k",
        api_version="2099-01-01", default_headers={"anthropic-beta": "prompt-caching"},
    )
    try:
        assert provider._client.headers["anthropic-version"] == "2099-01-01"
        assert provider._client.headers["anthropic-beta"] == "prompt-caching"
    finally:
        await provider.close()


async def test_gemini_default_headers():
    provider = GeminiProvider(
        model="m", api_key="k", default_headers={"x-proxy-auth": "secret"}
    )
    try:
        assert provider._client.headers["x-proxy-auth"] == "secret"
    finally:
        await provider.close()


# --- embedder batching -------------------------------------------------------


async def test_embedder_max_batch_size_chunks_transparently():
    calls = []

    def handler(request):
        import json as stdlib_json

        texts = stdlib_json.loads(request.read())["input"]
        calls.append(len(texts))
        return httpx.Response(200, json={
            "data": [{"embedding": [0.0]} for _ in texts]
        })

    client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler))
    embedder = OpenAICompatEmbedder(
        model="m", api_key="k", client=client, max_batch_size=3
    )
    result = await embedder.embed_batch([f"t{i}" for i in range(7)])
    assert len(result) == 7
    assert calls == [3, 3, 1]  # chunked, not one oversized request


# --- exact-match cache fingerprint now includes options/filters -------------


async def test_cache_fingerprint_distinguishes_provider_options():
    resolver = ContextResolver()
    ctx_a = RequestContext(input="same query")
    await resolver.execute(ctx_a)
    ctx_a.state["provider_options"] = {"temperature": 0.1}

    ctx_b = RequestContext(input="same query")
    await resolver.execute(ctx_b)
    ctx_b.state["provider_options"] = {"temperature": 0.9}

    lookup = CacheLookup(MemoryCache())
    assert lookup.fingerprint(ctx_a) != lookup.fingerprint(ctx_b)


async def test_cache_fingerprint_distinguishes_filters():
    resolver = ContextResolver()
    ctx_a = RequestContext(input="same query")
    await resolver.execute(ctx_a)
    ctx_a.state["filters"] = {"dept": "legal"}

    ctx_b = RequestContext(input="same query")
    await resolver.execute(ctx_b)
    ctx_b.state["filters"] = {"dept": "hr"}

    lookup = CacheLookup(MemoryCache())
    assert lookup.fingerprint(ctx_a) != lookup.fingerprint(ctx_b)


async def test_different_temperature_does_not_serve_wrong_cached_response():
    provider = FakeProvider(reply="response")
    runtime = Runtime(providers=[provider], cache=MemoryCache())
    await runtime.execute("q", temperature=0.1)
    await runtime.execute("q", temperature=0.9)
    assert provider.calls == 2  # both hit the provider; no false cache collision


async def test_extra_fingerprint_hook():
    resolver = ContextResolver()
    ctx_a = RequestContext(input="same query")
    await resolver.execute(ctx_a)
    ctx_a.state["tenant"] = "acme"

    ctx_b = RequestContext(input="same query")
    await resolver.execute(ctx_b)
    ctx_b.state["tenant"] = "globex"

    lookup = CacheLookup(MemoryCache(), extra_fingerprint=lambda c: c.state.get("tenant"))
    assert lookup.fingerprint(ctx_a) != lookup.fingerprint(ctx_b)


# --- bounded growth -----------------------------------------------------------


async def test_memory_cache_max_size_evicts_oldest():
    cache = MemoryCache(max_size=2)
    await cache.set("a", "1")
    await cache.set("b", "2")
    await cache.set("c", "3")
    assert await cache.get("a") is None  # evicted
    assert await cache.get("b") == "2"
    assert await cache.get("c") == "3"


async def test_memory_job_queue_maxsize_backpressures():
    import asyncio

    queue = MemoryJobQueue(maxsize=1)
    await queue.publish(Job(payload={"input": "1"}))
    put_second = asyncio.ensure_future(queue.publish(Job(payload={"input": "2"})))
    await asyncio.sleep(0.05)
    assert not put_second.done()  # blocked: queue is full
    await queue.pop(timeout=0.1)
    await asyncio.wait_for(put_second, timeout=1)


async def test_worker_shutdown_timeout_does_not_hang_forever():
    import asyncio

    class StuckProvider(FakeProvider):
        async def complete(self, messages, **options):
            await asyncio.sleep(10)
            return await super().complete(messages, **options)

    queue = MemoryJobQueue()
    await queue.publish(Job(payload={"input": "slow"}))
    runtime = Runtime(providers=[StuckProvider()])
    worker = RuntimeWorker(runtime, queue, concurrency=1, shutdown_timeout=0.2)
    run_task = asyncio.ensure_future(worker.run())
    await asyncio.sleep(0.05)
    worker.stop()
    await asyncio.wait_for(run_task, timeout=1)  # must return, not hang on the stuck job

    # shutdown_timeout intentionally leaves the timed-out job running in the
    # background rather than cancelling it; clean it up so it doesn't leak
    # across tests (and to prove it's exactly the one stuck _process task).
    assert len(worker._in_flight) == 1
    for leftover in worker._in_flight:
        leftover.cancel()
    await asyncio.gather(*worker._in_flight, return_exceptions=True)


# --- vector store extra options ----------------------------------------------


def make_client(handler):
    return httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler))


async def test_qdrant_score_threshold_and_search_params_and_with_vectors():
    captured = {}

    def handler(request):
        import json as stdlib_json

        captured.update(stdlib_json.loads(request.read()))
        return httpx.Response(200, json={"result": [
            {"id": 1, "score": 0.9, "payload": {}, "vector": [0.1, 0.2]}
        ]})

    store = QdrantVectorStore(
        collection="docs", client=make_client(handler),
        score_threshold=0.8, search_params={"hnsw_ef": 64}, with_vectors=True,
    )
    docs = await store.search([0.1])
    assert captured["score_threshold"] == 0.8
    assert captured["params"] == {"hnsw_ef": 64}
    assert captured["with_vector"] is True
    assert docs[0].embedding == [0.1, 0.2]


async def test_pinecone_include_values_and_sparse_vector():
    captured = {}

    def handler(request):
        import json as stdlib_json

        captured.update(stdlib_json.loads(request.read()))
        return httpx.Response(200, json={"matches": [
            {"id": "1", "score": 0.9, "metadata": {}, "values": [0.1, 0.2]}
        ]})

    store = PineconeVectorStore(
        host="https://x.test", api_key="k", client=make_client(handler),
        include_values=True, sparse_vector={"indices": [0], "values": [1.0]},
    )
    docs = await store.search([0.1])
    assert captured["includeValues"] is True
    assert captured["sparseVector"] == {"indices": [0], "values": [1.0]}
    assert docs[0].embedding == [0.1, 0.2]


# --- conflicting/misplaced config: fail loud, not silently ignored ----------


def test_build_cache_memory_with_url_raises_instead_of_dropping_it():
    from byoai.config import build_cache
    from byoai.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        build_cache({"provider": "memory", "url": "redis://leftover-from-a-provider-switch"})


def test_pgvector_pool_kwargs_colliding_with_typed_args_raises():
    from byoai.errors import ConfigurationError
    from byoai.vector.pgvector import PgVectorStore

    with pytest.raises(ConfigurationError):
        PgVectorStore(dsn="postgresql://x", table="docs", min_size=10)


def test_redis_sentinels_given_without_sentinel_mode_raises():
    from byoai.cache.redis import make_redis_client
    from byoai.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        make_redis_client(sentinels=[("host", 26379)], service_name="mymaster")


def test_pinecone_without_host_or_api_key_raises(monkeypatch):
    from byoai.errors import ConfigurationError
    from byoai.vector.pinecone import PineconeVectorStore

    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        PineconeVectorStore(host="https://x.test")  # api_key missing
    with pytest.raises(ConfigurationError):
        PineconeVectorStore(api_key="k")  # host missing


def test_azure_openai_without_api_key_raises(monkeypatch):
    from byoai.config import build_provider
    from byoai.errors import ConfigurationError

    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        build_provider({
            "provider": "azure_openai",
            "endpoint": "https://x.openai.azure.com",
            "deployment": "gpt-4o",
        })

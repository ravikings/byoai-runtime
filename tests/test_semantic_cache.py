from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from byoai import ConfigurationError, Runtime

numpy = pytest.importorskip("numpy")

from byoai.cache.semantic import MemorySemanticCache  # noqa: E402

# Toy embedder: maps known phrases to fixed directions so similarity is
# controllable. Unknown text gets an orthogonal direction.
_DIRECTIONS = {
    "what are our sla terms?": [1.0, 0.0, 0.0],
    "tell me about our slas": [0.98, 0.199, 0.0],  # ~0.98 cosine vs the first
    "how do i reset my password?": [0.0, 1.0, 0.0],
}


async def toy_embedder(text: str) -> list[float]:
    return _DIRECTIONS.get(text.lower(), [0.0, 0.0, 1.0])


def make_runtime(**kwargs) -> Runtime:
    return Runtime(
        providers=[FakeProvider(reply="the SLA answer")],
        semantic_cache={"provider": "memory", "threshold": 0.9, "capacity": 100},
        embedder=toy_embedder,
        **kwargs,
    )


async def test_store_find_respects_threshold():
    store = MemorySemanticCache(capacity=10)
    await store.add([1.0, 0.0, 0.0], "answer A")
    hit = await store.find([0.98, 0.199, 0.0], threshold=0.9)
    assert hit is not None and hit[0] == "answer A" and hit[1] > 0.9
    assert await store.find([0.0, 1.0, 0.0], threshold=0.9) is None


async def test_store_capacity_evicts_oldest():
    store = MemorySemanticCache(capacity=2)
    await store.add([1.0, 0.0], "one")
    await store.add([0.0, 1.0], "two")
    await store.add([0.7071, 0.7071], "three")  # evicts "one"
    assert await store.find([1.0, 0.0], threshold=0.99) is None
    assert (await store.find([0.0, 1.0], threshold=0.99))[0] == "two"


async def test_store_ttl_expiry():
    store = MemorySemanticCache(capacity=10, ttl=1)
    await store.add([1.0, 0.0], "fresh")
    assert (await store.find([1.0, 0.0], threshold=0.99))[0] == "fresh"
    store._expires[:] = 0.0  # force-expire without sleeping
    assert await store.find([1.0, 0.0], threshold=0.99) is None


async def test_intent_hit_serves_similar_query_without_provider_call():
    runtime = make_runtime()
    provider = runtime.router.providers[0]

    first = await runtime.execute("What are our SLA terms?")
    assert first.cached is False
    assert provider.calls == 1

    # different wording, same intent → served from the semantic cache
    second = await runtime.execute("Tell me about our SLAs")
    assert second.cached is True
    assert second.content == first.content
    assert second.context.metadata["semantic_cache_score"] > 0.9
    assert provider.calls == 1  # no second LLM call

    # genuinely different intent → goes to the provider
    third = await runtime.execute("How do I reset my password?")
    assert third.cached is False
    assert provider.calls == 2


async def test_intent_events_emitted():
    runtime = make_runtime()
    events: list[tuple[str, bool]] = []
    runtime.on("cache.*", lambda e, p: events.append((e, p.get("semantic", False))))
    await runtime.execute("What are our SLA terms?")
    await runtime.execute("Tell me about our SLAs")
    assert ("cache.miss", True) in events
    assert ("cache.hit", True) in events


async def test_streamed_response_feeds_intent_cache():
    runtime = make_runtime()
    provider = runtime.router.providers[0]
    _ = [c async for c in runtime.stream("What are our SLA terms?")]
    assert provider.calls == 1
    # similar query now hits the semantic cache, served as one chunk
    chunks = [c async for c in runtime.stream("Tell me about our SLAs")]
    assert provider.calls == 1
    text = "".join(c.delta for c in chunks if not c.done)
    assert text.strip() == "the SLA answer"


async def test_embedder_failure_degrades_to_miss_not_crash():
    from byoai.errors import ProviderError

    calls = {"n": 0}

    async def flaky_embedder(text: str) -> list[float]:
        calls["n"] += 1
        raise ProviderError("embeddings down", provider="emb", retryable=True)

    runtime = Runtime(
        providers=[FakeProvider()],
        semantic_cache={"provider": "memory", "threshold": 0.9},
        embedder=flaky_embedder,
    )
    misses = []
    runtime.on("cache.miss", lambda e, p: misses.append(p.get("error")))
    result = await runtime.execute("hi")  # must not raise
    assert result.content == "hello from fake"
    assert calls["n"] == 1
    assert misses and "embeddings down" in misses[0]


async def test_zero_magnitude_embedding_write_back_does_not_crash():
    async def zero_embedder(text: str) -> list[float]:
        return [0.0, 0.0, 0.0]

    runtime = Runtime(
        providers=[FakeProvider()],
        semantic_cache={"provider": "memory", "threshold": 0.9},
        embedder=zero_embedder,
    )
    result = await runtime.execute("hi")  # write-back of zero vector must not raise
    assert result.content == "hello from fake"


async def test_store_ttl_zero_means_do_not_store():
    store = MemorySemanticCache(capacity=10, ttl=0)
    await store.add([1.0, 0.0], "never stored")
    assert await store.find([1.0, 0.0], threshold=0.5) is None


async def test_store_reuse_after_close():
    store = MemorySemanticCache(capacity=10)
    for i in range(5):
        await store.add([1.0, float(i)], f"answer {i}")
    await store.close()
    await store.add([1.0, 0.0], "fresh")
    hit = await store.find([1.0, 0.0], threshold=0.99)
    assert hit is not None and hit[0] == "fresh"


async def test_semantic_cache_requires_embedder():
    with pytest.raises(ConfigurationError):
        Runtime(
            providers=[FakeProvider()],
            semantic_cache={"provider": "memory"},
        )


async def test_exact_cache_still_wins_before_semantic():
    from byoai.cache.memory import MemoryCache

    embed_calls = []

    async def counting_embedder(text: str) -> list[float]:
        embed_calls.append(text)
        return await toy_embedder(text)

    runtime = Runtime(
        providers=[FakeProvider()],
        cache=MemoryCache(),
        semantic_cache={"provider": "memory", "threshold": 0.9},
        embedder=counting_embedder,
    )
    await runtime.execute("What are our SLA terms?")
    await runtime.execute("What are our SLA terms?")  # exact hit
    # the second (identical) request never reached the embedding step
    assert len(embed_calls) == 1

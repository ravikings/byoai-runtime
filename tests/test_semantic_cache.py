from __future__ import annotations

import asyncio
from typing import cast

import pytest
from tests.conftest import FakeProvider

from byoai import ConfigurationError, Runtime
from byoai.errors import ProviderError

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


def make_runtime(*, providers=None, **kwargs) -> Runtime:
    return Runtime(
        providers=providers or [FakeProvider(reply="the SLA answer")],
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


async def test_store_metric_dot_no_normalization():
    # "dot" skips L2-normalization, so magnitude matters: a same-direction
    # vector with 10x the magnitude scores 10x higher, not ~1.0 like cosine.
    store = MemorySemanticCache(capacity=10, metric="dot")
    await store.add([1.0, 0.0], "small")
    hit = await store.find([10.0, 0.0], threshold=5.0)
    assert hit is not None and hit[0] == "small" and hit[1] == pytest.approx(10.0)


async def test_store_metric_euclidean_prefers_nearest():
    store = MemorySemanticCache(capacity=10, metric="euclidean")
    await store.add([0.0, 0.0], "near")
    await store.add([10.0, 10.0], "far")
    hit = await store.find([1.0, 0.0], threshold=-5.0)
    assert hit is not None and hit[0] == "near"


async def test_store_metric_unknown_name_rejected():
    with pytest.raises(ConfigurationError):
        MemorySemanticCache(metric="manhattan")  # type: ignore[arg-type]


async def test_store_metric_custom_callable():
    calls = []

    def score_first_dim_only(matrix, vector):
        calls.append(True)
        return matrix[:, 0]

    store = MemorySemanticCache(capacity=10, metric=score_first_dim_only)
    await store.add([1.0, 99.0], "a")
    await store.add([0.0, 0.0], "b")
    hit = await store.find([0.0, 0.0], threshold=0.5)
    assert hit is not None and hit[0] == "a"
    assert calls


async def test_store_capacity_evicts_oldest():
    store = MemorySemanticCache(capacity=2)
    await store.add([1.0, 0.0], "one")
    await store.add([0.0, 1.0], "two")
    await store.add([0.7071, 0.7071], "three")  # evicts "one"
    assert await store.find([1.0, 0.0], threshold=0.99) is None
    hit = await store.find([0.0, 1.0], threshold=0.99)
    assert hit is not None and hit[0] == "two"


async def test_store_ttl_expiry():
    store = MemorySemanticCache(capacity=10, ttl=1)
    await store.add([1.0, 0.0], "fresh")
    hit = await store.find([1.0, 0.0], threshold=0.99)
    assert hit is not None and hit[0] == "fresh"
    store._expires[:] = 0.0  # force-expire without sleeping
    assert await store.find([1.0, 0.0], threshold=0.99) is None


async def test_intent_hit_serves_similar_query_without_provider_call():
    runtime = make_runtime()
    assert runtime.router is not None
    provider = cast(FakeProvider, runtime.router.providers[0])

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
    assert runtime.router is not None
    provider = cast(FakeProvider, runtime.router.providers[0])
    _ = [c async for c in runtime.stream("What are our SLA terms?")]
    assert provider.calls == 1
    # similar query now hits the semantic cache, served as one chunk
    chunks = [c async for c in runtime.stream("Tell me about our SLAs")]
    assert provider.calls == 1
    text = "".join(c.delta for c in chunks if not c.done)
    assert text.strip() == "the SLA answer"


async def test_tool_only_streamed_turn_does_not_poison_intent_cache():
    # Regression: a tool-only streamed turn (no text deltas at all — every
    # chunk carries a tool_call, whose delta defaults to "") left
    # ctx.response == "" rather than None, and _write_back_cache only
    # excluded None. A later semantically-similar query then got served this
    # blank string as an intent-cache hit instead of triggering a fresh call.
    from tests.conftest import ToolCallingProvider

    runtime = make_runtime(providers=[ToolCallingProvider()])
    assert runtime.router is not None
    provider = cast(ToolCallingProvider, runtime.router.providers[0])

    _ = [c async for c in runtime.stream("What are our SLA terms?")]
    assert provider.calls == 1
    # A similar later query must NOT hit the (would-be-blank) intent cache —
    # it should reach the provider again, same as any other miss.
    _ = [c async for c in runtime.stream("Tell me about our SLAs")]
    assert provider.calls == 2


@pytest.mark.parametrize(
    "exc",
    [
        ProviderError("embeddings down", provider="emb", retryable=True),
        # Regression: embedder= accepts any user-supplied async (str) -> list[float]
        # callable, which won't necessarily raise our typed ByoAIError (e.g. a raw
        # ConnectionError/TimeoutError from the user's own HTTP client). The stage
        # previously only caught ByoAIError, so this leaked past and failed the
        # whole request instead of degrading to a cache miss.
        ConnectionError("dns lookup failed"),
    ],
    ids=["byoai-typed-error", "raw-exception"],
)
async def test_embedder_failure_degrades_to_miss_not_crash(exc):
    calls = {"n": 0}

    async def flaky_embedder(text: str) -> list[float]:
        calls["n"] += 1
        raise exc

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
    assert misses and str(exc) in misses[0]


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


async def test_find_survives_close_winning_a_concurrent_race():
    # Regression: find()'s pre-lock "is there anything to search" check can
    # go stale if a concurrent close() clears state before find() gets the
    # lock — without a re-check under the lock, find() would then read a
    # None matrix and crash instead of returning a clean miss.
    store = MemorySemanticCache(capacity=10)
    await store.add([1.0, 0.0], "answer")

    # Hold the mutex ourselves so close() and find() both queue behind it,
    # in a controlled order (asyncio.Lock is FIFO).
    await store._mutex.acquire()
    try:
        close_task = asyncio.create_task(store.close())
        await asyncio.sleep(0)  # let close() start waiting on the mutex first
        find_task = asyncio.create_task(store.find([1.0, 0.0], threshold=0.5))
        await asyncio.sleep(0)  # let find() queue behind close()
    finally:
        store._mutex.release()

    await close_task
    result = await find_task  # must degrade to a clean miss, not crash
    assert result is None


async def test_semantic_cache_requires_embedder():
    with pytest.raises(ConfigurationError):
        Runtime(
            providers=[FakeProvider()],
            semantic_cache={"provider": "memory"},
        )


async def test_semantic_cache_non_cosine_metric_requires_explicit_threshold():
    # DEFAULT_SEMANTIC_THRESHOLD (0.92) is calibrated for cosine; silently
    # falling back to it under e.g. "euclidean" (whose scores are <= 0)
    # would make every lookup miss forever with no signal to the caller.
    with pytest.raises(ConfigurationError):
        Runtime(
            providers=[FakeProvider()],
            semantic_cache={"provider": "memory", "metric": "euclidean"},
            embedder=toy_embedder,
        )


async def test_semantic_cache_plugin_with_non_cosine_default_metric_still_requires_threshold(
    monkeypatch,
):
    # Regression: the guard used to re-derive the metric default ("cosine")
    # from the raw config dict — semantic_cache.get("metric", "cosine") —
    # rather than the store the dict actually built. A plugin-provided
    # semantic cache whose own default metric isn't cosine, configured with
    # no "metric" key in the dict at all (nothing to read), used to skip the
    # guard entirely: "cosine" != "cosine" was always False.
    class FakeEntryPoint:
        name = "my_plugin"

        @staticmethod
        def load():
            return lambda config: FakePluginStore()

    class FakePluginStore:
        metric = "euclidean"  # this plugin's own non-cosine default

        async def find(self, embedding, *, threshold):
            return None

        async def add(self, embedding, response):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda *, group: [FakeEntryPoint()] if group == "byoai.semantic_caches" else [],
    )
    with pytest.raises(ConfigurationError):
        Runtime(
            providers=[FakeProvider()],
            semantic_cache={"provider": "my_plugin"},  # no "metric" key to read
            embedder=toy_embedder,
        )


async def test_semantic_cache_non_cosine_metric_with_explicit_threshold_is_fine():
    runtime = Runtime(
        providers=[FakeProvider()],
        semantic_cache={"provider": "memory", "metric": "euclidean", "threshold": -1.0},
        embedder=toy_embedder,
    )
    assert runtime.semantic_cache is not None


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

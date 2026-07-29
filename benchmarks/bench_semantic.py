"""Benchmark the semantic (intent) cache.

Measures lookup latency against cache size, and the end-to-end gap between
serving an intent hit vs going to a (simulated) LLM. Requires numpy
(`pip install byoai-runtime[semantic]`).

    .venv/bin/python benchmarks/bench_semantic.py
"""

from __future__ import annotations

import asyncio
import random
import time

from conftest_bench import FakeProvider

from byoai import Runtime
from byoai.cache.semantic import MemorySemanticCache

DIM = 768  # text-embedding-3-small dimensionality


def random_vector(rng: random.Random) -> list[float]:
    return [rng.gauss(0, 1) for _ in range(DIM)]


async def bench_lookup_latency() -> None:
    rng = random.Random(42)
    for size in (1_000, 10_000, 100_000):
        store = MemorySemanticCache(capacity=size, ttl=None)
        for i in range(size):
            await store.add(random_vector(rng), f"answer {i}")
        probe = random_vector(rng)
        n = 2_000
        start = time.perf_counter()
        for _ in range(n):
            await store.find(probe, threshold=0.99)
        per_lookup_us = (time.perf_counter() - start) / n * 1e6
        print(f"lookup vs {size:>7,} entries ({DIM}d):  {per_lookup_us:8.1f} µs/lookup")


class SlowProvider(FakeProvider):
    """Simulates a realistic LLM: 800ms per completion."""

    async def complete(self, messages, **options):
        await asyncio.sleep(0.8)
        return await super().complete(messages, **options)


async def bench_end_to_end() -> None:
    rng = random.Random(7)
    known = random_vector(rng)

    async def embedder(text: str) -> list[float]:
        # simulate a fast embedding API round-trip (~15ms)
        await asyncio.sleep(0.015)
        return known if "sla" in text.lower() else random_vector(rng)

    runtime = Runtime(
        providers=[SlowProvider(reply="Our enterprise SLA terms are ...")],
        semantic_cache={"provider": "memory", "threshold": 0.9},
        embedder=embedder,
    )

    start = time.perf_counter()
    await runtime.execute("What are our SLA terms?")
    cold = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    result = await runtime.execute("Tell me about our enterprise SLAs")
    warm = (time.perf_counter() - start) * 1000
    assert result.cached, "expected an intent hit"

    print(f"\ncold (LLM 800ms + embed 15ms):     {cold:8.1f} ms")
    print(f"intent hit (embed 15ms + lookup):  {warm:8.1f} ms   ({cold / warm:.0f}x faster)")


async def main() -> None:
    await bench_lookup_latency()
    await bench_end_to_end()


if __name__ == "__main__":
    asyncio.run(main())

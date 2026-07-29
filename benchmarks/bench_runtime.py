"""In-process micro-benchmark: the runtime's own per-request overhead.

Uses a null provider (returns instantly) so everything measured is ByoAI —
pipeline, middleware, events, context, cache fingerprinting. This is the
number that must stay small: it is what ByoAI adds on top of your LLM call.

    .venv/bin/python benchmarks/bench_runtime.py
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from typing import Any

from bench_common import percentile

from byoai import Message, ProviderResponse, Runtime, Usage
from byoai.cache.memory import MemoryCache


class NullProvider:
    name = "null"
    model = "null-1"

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        return ProviderResponse(
            content="ok", model=self.model, provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def stream(self, messages, **options):
        from byoai import StreamChunk

        for _ in range(8):
            yield StreamChunk(delta="tok ", model=self.model, provider=self.name)
        yield StreamChunk(done=True, model=self.model, provider=self.name, usage=Usage(10, 5))

    async def close(self) -> None:
        pass


async def bench(name: str, runtime: Runtime, *, n: int, concurrency: int,
                unique_inputs: bool) -> None:
    latencies: list[float] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def one(i: int) -> None:
        async with semaphore:
            start = time.perf_counter()
            await runtime.execute(f"question {i}" if unique_inputs else "question")
            latencies.append((time.perf_counter() - start) * 1000)

    wall_start = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(n)))
    wall = time.perf_counter() - wall_start

    latencies.sort()
    print(
        f"{name:<42} {n / wall:>9.0f} req/s   "
        f"mean {statistics.mean(latencies):6.3f} ms   "
        f"p50 {percentile(latencies, 0.50):6.3f}   "
        f"p95 {percentile(latencies, 0.95):6.3f}   "
        f"p99 {percentile(latencies, 0.99):6.3f}"
    )


async def bench_stream(runtime: Runtime, *, n: int, concurrency: int) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    chunks = 0

    async def one(i: int) -> int:
        async with semaphore:
            count = 0
            async for _ in runtime.stream(f"question {i}"):
                count += 1
            return count

    wall_start = time.perf_counter()
    results = await asyncio.gather(*(one(i) for i in range(n)))
    wall = time.perf_counter() - wall_start
    chunks = sum(results)
    print(
        f"{'stream (9 chunks/req)':<42} {n / wall:>9.0f} req/s   "
        f"{chunks / wall:>9.0f} chunks/s"
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=20_000)
    parser.add_argument("-c", "--concurrency", type=int, default=200)
    args = parser.parse_args()

    async def noop_middleware(ctx, call_next):
        await call_next(ctx)

    bare = Runtime(providers=[NullProvider()])
    with_cache = Runtime(providers=[NullProvider()], cache=MemoryCache())
    loaded = Runtime(providers=[NullProvider()], cache=MemoryCache())
    for _ in range(5):
        loaded.use(noop_middleware)
    loaded.on("request.*", lambda e, p: None)
    loaded.on("provider.*", lambda e, p: None)

    print(f"n={args.n} concurrency={args.concurrency}  (overhead-only: null provider)\n")
    await bench("bare runtime (pipeline+events)", bare,
                n=args.n, concurrency=args.concurrency, unique_inputs=True)
    await bench("with cache, all misses (fingerprint+store)", with_cache,
                n=args.n, concurrency=args.concurrency, unique_inputs=True)
    await bench("with cache, all hits (short-circuit)", with_cache,
                n=args.n, concurrency=args.concurrency, unique_inputs=False)
    await bench("5 middleware + 4 event subscriptions", loaded,
                n=args.n, concurrency=args.concurrency, unique_inputs=True)
    await bench_stream(bare, n=args.n // 4, concurrency=args.concurrency)


if __name__ == "__main__":
    asyncio.run(main())

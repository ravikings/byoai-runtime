"""Benchmark the JSON-hot paths: cache fingerprinting, SSE/WS framing,
provider stream-line parsing.

Run once before and once after enabling orjson to see the delta:

    .venv/bin/python benchmarks/bench_json.py            # uses orjson if installed
    BYOAI_JSON=std .venv/bin/python benchmarks/bench_json.py   # force stdlib json
"""

from __future__ import annotations

import asyncio
import time

from byoai import Runtime
from byoai._json import backend_name, dumps, loads
from byoai.cache.memory import MemoryCache
from byoai.context import RequestContext
from byoai.stages import CacheLookup, ContextResolver
from byoai.transport import sse_stream, ws_reply


def rate(n: int, wall: float) -> str:
    return f"{n / wall:>12,.0f} ops/s"


async def main() -> None:
    print(f"json backend: {backend_name()}\n")

    # -- fingerprint (runs once per cached execute) ---------------------------
    resolver = ContextResolver()
    ctx = RequestContext(input="what are our enterprise SLA terms, in detail?")
    await resolver.execute(ctx)
    ctx.model = "gpt-4o"
    n = 200_000
    start = time.perf_counter()
    for _ in range(n):
        CacheLookup.fingerprint(ctx)
    print(f"cache fingerprint            {rate(n, time.perf_counter() - start)}")

    # -- raw dumps/loads of a typical frame -----------------------------------
    frame = {"delta": "hello world, this is a token batch of text"}
    result = {
        "content": "x" * 400, "cached": False, "model": "gpt-4o",
        "provider": "openai",
        "usage": {"input_tokens": 812, "output_tokens": 220, "cost_usd": 0.0114},
        "request_id": "a" * 32,
    }
    n = 500_000
    start = time.perf_counter()
    for _ in range(n):
        dumps(frame)
    print(f"dumps small frame            {rate(n, time.perf_counter() - start)}")
    start = time.perf_counter()
    for _ in range(n):
        dumps(result)
    print(f"dumps result dict            {rate(n, time.perf_counter() - start)}")
    encoded = dumps(result)
    start = time.perf_counter()
    for _ in range(n):
        loads(encoded)
    print(f"loads result dict            {rate(n, time.perf_counter() - start)}")

    # -- provider SSE line parse (runs once per streamed token batch) ---------
    line = dumps({
        "model": "gpt-4o", "choices": [{"delta": {"content": "tok"}}],
        "usage": None,
    })
    n = 500_000
    start = time.perf_counter()
    for _ in range(n):
        loads(line)
    print(f"provider SSE line parse      {rate(n, time.perf_counter() - start)}")

    # -- end-to-end transport framing (streaming through the runtime) --------
    from conftest_bench import FakeProvider

    runtime = Runtime(providers=[FakeProvider(reply="tok " * 16)], cache=MemoryCache())
    n = 5_000
    start = time.perf_counter()
    frames = 0
    for i in range(n):
        async for _ in sse_stream(runtime, {"input": f"q{i}"}):
            frames += 1
    wall = time.perf_counter() - start
    print(f"sse_stream end-to-end        {rate(frames, wall)} (frames)")
    start = time.perf_counter()
    frames = 0
    for i in range(n):
        async for _ in ws_reply(runtime, dumps({"input": f"stream {i}"})):
            frames += 1
    wall = time.perf_counter() - start
    print(f"ws_reply end-to-end          {rate(frames, wall)} (frames)")


if __name__ == "__main__":
    asyncio.run(main())

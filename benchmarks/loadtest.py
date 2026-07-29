"""HTTP load generator for ByoAI transport endpoints.

Async httpx clients hammer one endpoint with bounded concurrency and report
throughput + latency percentiles. Point it at the FastAPI example, the Robyn
example, or any deployment.

    .venv/bin/python benchmarks/loadtest.py http://127.0.0.1:9932/ask \
        --body '{"query": "hi"}' -n 5000 -c 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx
from bench_common import percentile


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--body", default='{"input": "hello"}')
    parser.add_argument("-n", type=int, default=2000, help="total requests")
    parser.add_argument("-c", "--concurrency", type=int, default=100)
    parser.add_argument("--clients", type=int, default=8, help="httpx client pool size")
    args = parser.parse_args()

    body = json.loads(args.body)
    latencies: list[float] = []
    errors: dict[str, int] = {}
    semaphore = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    clients = [httpx.AsyncClient(limits=limits, timeout=30) for _ in range(args.clients)]

    async def one(i: int) -> None:
        async with semaphore:
            start = time.perf_counter()
            try:
                response = await clients[i % len(clients)].post(args.url, json=body)
                if response.status_code != 200:
                    errors[f"http {response.status_code}"] = (
                        errors.get(f"http {response.status_code}", 0) + 1
                    )
                    return
            except Exception as exc:
                errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
                return
            latencies.append((time.perf_counter() - start) * 1000)

    # short warmup so connection setup isn't in the measured window
    await asyncio.gather(*(one(i) for i in range(min(100, args.n))))
    latencies.clear()
    errors.clear()  # warmup failures must not pollute the measured report

    wall_start = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(args.n)))
    wall = time.perf_counter() - wall_start
    for client in clients:
        await client.aclose()

    if not latencies:
        print(f"all {args.n} requests failed: {errors}")
        return
    latencies.sort()
    print(f"target      {args.url}")
    print(f"requests    {args.n}  concurrency {args.concurrency}  wall {wall:.2f}s")
    print(f"throughput  {len(latencies) / wall:.0f} req/s")
    print(
        f"latency ms  mean {statistics.mean(latencies):.2f}  "
        f"p50 {percentile(latencies, 0.5):.2f}  p95 {percentile(latencies, 0.95):.2f}  "
        f"p99 {percentile(latencies, 0.99):.2f}  max {latencies[-1]:.2f}"
    )
    if errors:
        print(f"errors      {errors}")


if __name__ == "__main__":
    asyncio.run(main())

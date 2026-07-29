# Semantic (intent) caching

Requires the `semantic` extra for the in-process store: `pip install "byoai-runtime[semantic]"`.
The shared Redis-backed store additionally requires `redis`: `pip install "byoai-runtime[redis,semantic]"`.

The exact-match [response cache](caching.md) only short-circuits byte-identical requests.
`semantic_cache=` adds a second cache stage that embeds the query and matches it against
previously answered queries by cosine similarity — `"What are our SLA terms?"` can be served
from the cached answer to `"Tell me about our enterprise SLAs"` without an LLM call.

Economics: one embedding call (roughly 5-50ms, ~$0.00002) replaces one LLM call (hundreds of
milliseconds to seconds, 100-1000× the cost) whenever intent matches closely enough. `threshold`
tunes that tolerance — 0.95+ is conservative; below ~0.85 risks serving answers to genuinely
different questions.

`semantic_cache=` requires an `embedder=` — see the [Providers guide](providers.md#embeddings).

## In-process (per-worker)

```python
runtime = Runtime(
    llm={"provider": "openai", "model": "gpt-4o"},
    embedder={"provider": "openai", "model": "text-embedding-3-small"},
    semantic_cache={"provider": "memory", "capacity": 10_000, "ttl": 3600, "threshold": 0.92},
)
```

`byoai.cache.semantic.MemorySemanticCache` is numpy-accelerated brute-force cosine similarity
over normalized vectors — exact (not approximate), fast up to roughly 100k entries. It's a fixed
ring buffer: `capacity` bounds memory, oldest entries evict first. `ttl` is wall-clock seconds
per entry (`None` disables expiry).

## Shared across workers (Redis)

```python
runtime = Runtime(
    llm={"provider": "openai", "model": "gpt-4o"},
    embedder={"provider": "openai", "model": "text-embedding-3-small"},
    semantic_cache={
        "provider": "redis",
        "url": "redis://redis.internal:6379",
        "stream": "byoai:semcache",
        "capacity": 10_000,
        "ttl": 3600,
        "threshold": 0.92,
    },
)
```

`byoai.cache.semantic.RedisSemanticCache` stores entries on one Redis Stream under the isolated
`byoai:` namespace (embedding packed as base64 float32, response, and wall-clock expiry). Every
worker keeps a local numpy mirror and catches up incrementally before each lookup — usually an
empty round-trip — so intent hits are shared across processes/replicas and survive restarts,
while the similarity math itself stays local and fast. It accepts the same `mode`/`sentinels`/
`service_name` options as [`RedisCache`](caching.md#redis) for standalone/cluster/Sentinel
deployments.

## Failure handling

A semantic-cache or embedder failure degrades to a cache miss rather than failing the request —
the same "infrastructure blip can't take down execution" principle as the exact-match cache (see
[Caching](caching.md#cache-ttl-for-responses)).

## Custom stores

An unrecognized `semantic_cache=` `provider` is resolved through the `byoai.semantic_caches`
plugin group, same as vector stores and LLM providers — see
[Vector stores: custom adapters via plugins](vector-stores.md#custom-adapters-via-plugins).

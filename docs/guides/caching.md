# Caching

`byoai.cache` adapters cover two distinct, deliberately-combined concerns — they share a
connection to the same existing infrastructure:

- **Runtime cache** (read-write) handles the exact-match response cache, planner state, and
  execution artifacts. All writes stay under an isolated namespace (default `byoai:`), so ByoAI
  never collides with your application's own keys.
- **Session reader** (read-only), by contrast, only ingests *existing* application state — chat
  history your app already stores in Redis, say — through a key-pattern mapping; ByoAI never
  writes through this path.

## In-memory (dev/tests)

```python
runtime = Runtime(cache={"provider": "memory"})
```

Backed by `byoai.cache.memory.MemoryCache` — no external dependency, state is lost on process
exit.

## Redis

Requires the `redis` extra: `pip install "byoai-runtime[redis]"`.

```python
runtime = Runtime(
    cache={
        "provider": "redis",
        "url": "redis://redis.internal:6379",
        "namespace": "byoai:",       # all writes go to byoai:cache:*, byoai:planner:*, ...
        "default_ttl": 3600,
        "session_reader": {
            "pattern": "session:{user_id}:messages",  # fills {user_id} from execute(user_id=...)
            "format": "json",
        },
    },
)
```

`provider: "valkey"` uses the same adapter — Valkey speaks the Redis protocol.

`mode` selects the deployment topology: `"standalone"` (default, single node or Valkey),
`"cluster"` (Redis Cluster, `url` pointing at any node), or `"sentinel"` (pass `sentinels` as
`[(host, port), ...]` plus `service_name`). Extra keyword arguments (`socket_timeout`,
`ssl`, `ssl_ca_certs`, ...) are forwarded to the underlying `redis-py` client.

You can also pass an already-constructed client instead of a dict, or use the adapter classes
directly for full control — see the [API reference](../reference/api.md).

## Semantic (intent) caching

For serving *similar*, not just identical, queries from cache — see the dedicated
[Semantic caching guide](semantic-cache.md).

## Cache TTL for responses

There's no separate `Runtime`-level TTL knob — a written-back response cache entry lives for
however long the cache itself is configured to keep entries: `cache={"default_ttl": 3600}`
(seconds; `None` disables expiry), or the matching constructor arg if you're passing a pre-built
`CacheStore` instance directly. Setting it in exactly one place avoids the previous split where a
`Runtime(cache_ttl=...)` default silently overrode whatever `default_ttl` the cache was already
configured with. `MemoryCache`/`RedisCache` both default `default_ttl` to `3600` on their own, so
leaving it unset still expires entries after an hour rather than caching forever — pass
`default_ttl=None` explicitly if you actually want entries to never expire, or `<=0` if you want
to disable response caching entirely (writes silently no-op instead of storing anything).

A cache outage on write-back never fails the request — the `CacheError` is caught and logged
(`logger.warning`, not silently discarded) so a cache blip can't take down execution but is still
visible in application logs.

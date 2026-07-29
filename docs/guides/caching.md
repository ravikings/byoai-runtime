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

`Runtime(..., cache_ttl=3600)` controls how long a written-back response cache entry lives
(seconds; `None` disables expiry). A cache outage on write-back never fails the request — the
`CacheError` is caught and silently discarded, by design, so a cache blip can't take down
execution. (There is currently no logging of the discarded error — don't rely on application
logs to surface a failing cache write-back.)

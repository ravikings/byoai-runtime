# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: minor versions may include breaking changes).

## [Unreleased]

Nothing yet.

## [0.1.0a3] - 2026-08-07

### Fixed
- Capped the core dependency at `httpx>=0.25,<1.0`. Installing an alpha requires `pip install
  --pre`, and `--pre` allows pre-releases for *every* package in the resolve, not just this one —
  so the uncapped requirement pulled `httpx 1.0.dev3`, whose API no longer has `httpx.Timeout`.
  A clean `pip install --pre "byoai-runtime[agent-context-cache]"` on 0.1.0a2 therefore installed
  a `byoai-cache` that raised `AttributeError` on import. Same hazard applies to any dependency
  that publishes a pre-release; this is the one that bit.

## [0.1.0a2] - 2026-08-07

### Fixed
- Docs told you to install the `byoai-cache` proxy with a bare `pip install byoai-runtime`, which
  produces a console script that fails on import: the proxy needs FastAPI, uvicorn, and redis, and
  the base package depends only on `httpx`. The correct install is
  `pip install --pre "byoai-runtime[agent-context-cache]"`, now used in the README, CONFIGURATION,
  and the getting-started extras list (which was also missing the `flask` and `agent-context-cache`
  extras entirely).

### Breaking
- Removed `Runtime(cache_ttl=...)`. TTL is now configured in exactly one place —
  `cache={"default_ttl": ...}`, or the matching arg on a pre-built `CacheStore`.
  `MemoryCache`/`RedisCache` now default `default_ttl` to `3600` (was `None`), so a cache
  built with no `default_ttl` no longer caches forever by accident.

### Added
- `BYOAI_RETENTION_DAYS` (default `90`) caps how long rows live in the proxy's durable SQLite
  log, applied once per proxy start. Previously the log was append-only with no ceiling, so the
  file and the unfiltered `SUM()` queries behind `/v1/stats/permanent` both grew with uptime.
  `0` restores the keep-forever behavior.
- `byoai-cache prune [--days N] [--no-vacuum]` applies the retention window on demand, for a
  proxy that has been running for months without a restart. The delete is safe against a live
  instance; reclaiming the freed space is not, so pass `--no-vacuum` (or stop the proxy) if one
  is running — `VACUUM` takes an exclusive lock that WAL mode does not let readers through.

- `docs/guides/testing.md` — testing a `Runtime`-based app without a real API key, Redis, or
  Postgres, using the existing in-process adapters (`providers=`/`vector_store=`/`embedder=`
  bare functions, `cache={"provider": "memory"}`, `MemoryJobQueue`).
- `Message` gained `tool_call_id`/`tool_calls`, completing the OpenAI-compatible tool-calling
  round trip (sending a tool result back). See `docs/guides/providers.md`.
- `MemorySemanticCache`/`RedisSemanticCache` and `PgVectorStore` gained `metric=`
  (`cosine`/`dot`/`euclidean` or `cosine`/`l2`/`inner_product` respectively, plus a custom
  callable for the semantic caches) instead of a hardcoded metric.
- `ProviderRouter`/`Runtime` gained `selection=` (`"ordered"` default, `"round_robin"`, or a
  custom callable) controlling which provider is tried first each call.
- `byoai.integrations.flask` (`attach`/`get_runtime`/`execute`/`stream_response`) — a Flask
  (WSGI) bridge to `Runtime`'s asyncio-native providers via a background event-loop thread.
  Requires the new `flask` extra; see `docs/guides/flask.md`.
- `Runtime.execute()`/`.stream()` gained `system_prompt=` (per-call override) and
  `provider_metadata=` (forwarded into the provider's own request payload, e.g. Anthropic's
  `metadata.user_id`).
- `Message.content` now accepts a list of provider content blocks, not just plain text;
  `ExecutionResult` gained `finish_reason`/`raw`. Anthropic/Bedrock/Vertex handle list-valued
  content today — see `docs/guides/providers.md`.
- `StreamChunk` gained `tool_call`, so a forced-`tool_choice` streaming call now actually
  yields content instead of nothing. Fixed across Anthropic, Bedrock/Vertex,
  OpenAI-compatible, and every transport (`transport.stream_frames()`, FastAPI/Flask SSE).
- `AnthropicProvider`/`AnthropicBedrockProvider`/`AnthropicVertexProvider` gained
  `cache_system=` for Anthropic's server-side prompt caching, and now parse
  `cache_read_input_tokens`/`cache_creation_input_tokens` into `Usage`.
- `providers=`/`vector_store=` now accept a bare async function directly — no class required —
  matching the existing `embedder=`/`Pipeline.add()` pattern.
- `AnthropicBedrockProvider`/`AnthropicVertexProvider` (`llm={"provider": "bedrock"/"vertex"}`)
  — Anthropic models via AWS Bedrock or Google Vertex AI. New `bedrock`/`vertex` extras.
- `PipelineNotFoundError`/`AllProvidersFailedError` — PEP 8-compliant names for the two
  exceptions that lacked the `Error` suffix (old names remain as aliases).
- `Runtime.aclose()` as an alias for `close()`.
- All library-owned HTTP clients send a `User-Agent: byoai-runtime/<version>` header.
- `default_params=` on `OpenAICompatProvider`/`build_openai_client` for query parameters
  applied to every request (how the Azure preset now passes `api-version`).
- Library logging on the `byoai.*` loggers (silent by default via `NullHandler`).
- CI now measures test coverage; the release workflow runs the full test matrix before
  publishing and pins the PyPI publish action to a commit SHA; builds are checked with
  `twine check --strict`.

### Changed
- `InMemoryHashStore` now caps hashes per session (`max_hashes_per_session`, default 5,000,
  oldest evicted first) in addition to the existing session cap, so peak memory is bounded by
  the two caps rather than growing with the length of any single conversation.
- `RedisHashStore` keeps mirroring every add into its in-memory fallback. Skipping the mirror
  while Redis is healthy would save memory, but it makes correctness depend on detecting
  recovery and replaying state between two stores; that was implemented, reviewed, and reverted
  after each edge case (partial failures, per-session vs. store-wide health, TTL expiry,
  concurrent writes mid-replay) turned out to be a way to silently report already-sent content
  as new. The mirror's cost is bounded by the `InMemoryHashStore` caps above.
- README's Quickstart now leads with a minimal "Hello world" example before the fuller
  "Production Setup" one.
- `build_cache`, `PgVectorStore`, `make_redis_client`, and the `azure_openai`/`pinecone`
  provider presets now raise `ConfigurationError` on invalid or missing config (a stale `url`
  left over from switching `provider` to `"memory"`, `min_size`/`max_size` pool kwargs colliding
  with the typed constructor args, `sentinels`/`service_name` given without `mode="sentinel"`,
  missing API keys) instead of silently ignoring it or failing later with an opaque error.
- `transport.parse_payload` raises `ConfigurationError` when a top-level field (e.g. `model`)
  collides with the same key inside `options`, instead of letting `options` silently win.
- `OpenAICompatEmbedder.embed_batch()` reorders responses by each item's own `"index"` field
  instead of trusting response order, fixing a silent vector/text mismatch against backends
  that return embeddings out of request order.
- README's vector-database list now only lists adapters that actually ship (pgvector,
  Pinecone, Qdrant), with a pointer to the `byoai.vector_stores` plugin mechanism for the rest.
- `Pipeline.remove()`/`.replace()` accept `name=` to target one stage among several of the same
  `stage_type`. Calling either with no matching criteria now raises `ConfigurationError`
  instead of silently matching nothing.
- Documented two known `RedisStreamQueue`/`RuntimeWorker` gaps in `docs/guides/workers.md`: no
  reclaim (`XCLAIM`/`XAUTOCLAIM`) for a crashed consumer's pending entries, and `RuntimeWorker`
  doesn't own or close the queue's connection.
- CI now gates on `pyright` (previously advisory); fixed ~60 pre-existing type errors this
  surfaced, mostly Optional-narrowing gaps in the Redis-backed adapters.
- `AnthropicProvider`/`GeminiProvider`'s construction-time API-key check now only backs off for
  an actual auth header in `default_headers=`, not any unrelated header.
- `PgVectorStore.search()` offloads embedding-literal construction to a thread above 1024
  dimensions, avoiding event-loop stalls on large vectors, and hoists invariant SQL parts out
  of the per-call query string.
- Version is now single-sourced from `src/byoai/_version.py` (`pyproject.toml` reads it
  dynamically), so `byoai.__version__` can no longer drift from the published package version.
- Robyn integration maps runtime errors to real HTTP statuses (`400`/`404`/`429`/`502`) instead
  of a blanket `422`.
- `AnthropicProvider`/`GeminiProvider` raise `ConfigurationError` at construction when neither
  an API key nor `default_headers` are supplied, instead of failing later with a 401.
- `Retry-After` headers in RFC 9110 HTTP-date form are now honored, not just delay-seconds form.
- pgvector filter compiler binds metadata field names as query parameters instead of splicing
  them into SQL text.
- OTel spans use GenAI semantic-convention attribute keys (`gen_ai.*`) — dashboards filtering on
  the old ad-hoc keys (`provider`, `model`, `input_tokens`) need updating.
- `MemorySemanticCache` runs its similarity math in a worker thread past 4096 entries.
- `ExecutionResult.context`/`RequestContext.documents` are precisely typed; `Runtime`'s
  `embedder=`/`semantic_cache=` are typed against their protocols.

### Fixed
- `ProviderRouter.complete()`/`.stream()` pass a shallow copy of `self.providers` to a custom
  `selection=` callable instead of the live list, so an in-place-mutating callable can no
  longer corrupt the router's provider order for every future call.
- `Runtime`'s non-cosine-metric threshold guard now reads the semantic cache's actually-resolved
  metric instead of re-deriving a `"cosine"` default, fixing a silent always-miss for
  plugin caches whose default metric isn't cosine.
- `RuntimeWorker._process()` no longer silently swallows an exception from
  `queue.push_result()`/`.ack()` raised after the runtime already answered; now logged and
  counted via the new `RuntimeWorker.errors`.
- Streaming adapters (OpenAI-compatible, Anthropic, Gemini) now raise `ProviderError` on an
  in-band `error` event mid-stream, instead of silently delivering a truncated generation as a
  clean success.
- FastAPI's `stream_response()` turns a mid-stream runtime error into a clean terminal SSE
  event instead of tearing the connection.
- Azure OpenAI preset passes `api-version` as a real query parameter (previously baked into
  `base_url`, where it broke httpx's path concatenation).
- `byoai.integrations.flask`'s bridge no longer hangs a request thread on shutdown or leaks
  provider connections behind in-flight/abandoned streams. `close()` now cancels pending
  futures, finalizes idle streams concurrently, and bounds total shutdown time to the
  requested `timeout` instead of stacking per-phase timeouts.
- `ContextResolver`'s session-history coercion no longer turns a tool-call-only assistant
  turn's `content: null` into the literal text `"None"`. That turn and its now-unanswerable
  `"tool"` replies are dropped, and adjacent same-role messages are merged at the resulting
  seam so truncation/dropping can't leave a provider-rejected same-role/wrong-starting-role
  history.
- Synchronous `execute()` now catches `concurrent.futures.CancelledError` and wraps it in
  `ByoAIError`, matching `stream_response()`'s existing behavior.
- `stream_response()`'s SSE loop no longer mislabels a genuine empty-message `ByoAIError` as
  `"request cancelled"`.

### Security
- `PgVectorStore` redacts a DSN's embedded password (`postgresql://user:pass@host` →
  `postgresql://***@host`) before a connection failure can leak it into an error message.

## [0.1.0a1] - 2026-07-29

Initial alpha release.

### Added
- Core runtime: `Runtime`, `Pipeline`/`PipelineStage`, `Middleware`, request context, and the
  structured error hierarchy (`ByoAIError`, `ProviderError`, `RateLimitError`, `AllProvidersFailed`, etc.).
- Provider router with fallback/failover, `httpx`-based OpenAI-compatible, Anthropic, and Gemini
  providers, plus an `embedder=` adapter for any OpenAI-compatible `/embeddings` endpoint.
- Cache adapters: in-memory and Redis (standalone/cluster/Sentinel), with namespaced isolation
  from existing application keys.
- Semantic (intent) cache — serve similar, not just identical, queries from cache via embedding
  similarity, in-process (`MemorySemanticCache`) or shared across workers on Redis
  (`RedisSemanticCache`).
- Vector store adapters for pgvector, Qdrant, and Pinecone, including a cross-provider AST filter
  parser and a `VectorRetrieve` pipeline stage for retrieval-augmented generation.
- Plugin system: unrecognized `provider` values for `llm=`, `cache=`, `vector_store=`,
  `embedder=`, and `semantic_cache=` resolve through Python entry points.
- OpenTelemetry tracing (`byoai.telemetry.otel`): one span per execution, per-stage child spans,
  provider lifecycle span events, and OTLP export (gRPC or HTTP) to an existing collector.
- `orjson`-backed hot-path JSON codec (`perf` extra) for lower-overhead request/response encoding.
- FastAPI integration (`byoai.integrations.fastapi`): `attach`, `get_runtime`, SSE `stream_response`.
- Robyn integration, WebSocket transport, and background queue workers.
- MCP integration (`byoai.integrations.mcp`): expose a runtime as an MCP tool server (`execute` /
  `execute_stream` tools) over stdio or streamable HTTP, standalone or mounted into an existing
  FastAPI/Starlette app.
- `CONFIGURATION.md`: authoritative parameter-by-parameter reference across every component.
- Provider adapters gained `retryable_status=` overrides and gateway-friendly path overrides
  (`chat_path=`/`messages_path=`/`embeddings_path=`); the embeddings adapter transparently chunks
  large `embed_batch()` calls via `max_batch_size=`.
- `RuntimeWorker` gained a `shutdown_timeout=` for bounded graceful shutdown;
  `MemoryJobQueue`/`RedisStreamQueue` gained backpressure/trim controls (`maxsize=`, `maxlen=`).
- Benchmark suite for JSON encoding, runtime throughput, and multi-process Robyn scaling
  (72.5k req/s aggregate across 8 processes on the cache-hit path).

### Fixed
- `byoai.integrations.robyn`'s SSE stream route (`POST /byoai/stream`) crashed with
  `AttributeError: 'dict' object has no attribute 'set'` on every call — it passed a plain dict
  as `StreamingResponse(headers=...)`, but Robyn's default-SSE-header code calls `.set()` on it,
  which only Robyn's own `Headers` type supports. Wrapped in `Headers(...)`.
- A malformed (non-JSON) 200 response — e.g. a misconfigured gateway returning an HTML error
  page — leaked a raw `json.JSONDecodeError` past every provider adapter (`OpenAICompatProvider`,
  `AnthropicProvider`, `GeminiProvider`, `OpenAICompatEmbedder`) instead of a `ProviderError`,
  breaking the router's retry/fallback and error-typing contract. Added a shared
  `parse_json_response()` helper used by all four.
- `SemanticCacheLookup` only caught `ByoAIError` around the embedder/store call, so a
  user-supplied `embedder=` callable (explicitly a supported use case — any
  `async (str) -> list[float]`) raising a plain exception (`ConnectionError`, `TimeoutError`,
  etc.) failed the whole request instead of degrading to a cache miss, contradicting the
  documented "a semantic-cache or embedder hiccup must never fail a request" guarantee.
  Broadened to catch any exception.

[Unreleased]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a3...HEAD
[0.1.0a3]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a2...v0.1.0a3
[0.1.0a2]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/ravikings/byoai-runtime/releases/tag/v0.1.0a1

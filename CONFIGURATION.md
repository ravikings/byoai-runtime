# Configuration Reference

Every knob in ByoAI Runtime, by component. Two ways to configure anything:

1. **Declarative dicts** — `Runtime(llm={...}, cache={...}, ...)`. Easiest to
   load from env/JSON/YAML; goes through `byoai/config.py`'s `build_*`
   functions, which `**kwargs`-forward straight into the class below.
2. **Direct objects** — construct the adapter yourself (`OpenAICompatProvider(...)`,
   `RedisCache(...)`, ...) and pass the instance in
   (`Runtime(providers=[...], cache=my_cache, ...)`). Same parameters either way.

Unless noted, every parameter has a default that preserves prior behavior —
nothing here is required to keep working code working.

---

## Runtime

`byoai.Runtime(...)`

| Param | Default | Notes |
| --- | --- | --- |
| `llm` | `None` | Dict: `{"provider": ..., "model": ..., "fallback": {...}}`. See [Providers](#providers). |
| `providers` | `None` | Pre-built `LLMProvider` list instead of/in addition to `llm=`. |
| `cache` | `None` | Dict or `CacheStore` instance. See [Cache](#cache-exact-match). |
| `vector_store` | `None` | Dict or `VectorStore` instance. See [Vector stores](#vector-stores). Building it alone doesn't add retrieval to the default pipeline — see [RAG retrieval in the pipeline](guides/vector-stores.md#rag-retrieval-in-the-pipeline) for wiring in `VectorRetrieve`. |
| `semantic_cache` | `None` | Dict or `SemanticCacheStore` instance. See [Semantic cache](#semantic-intent-cache). Requires `embedder=`. |
| `embedder` | `None` | Dict or async `(str) -> list[float]` callable. Powers `semantic_cache` and `vector_store` retrieval. |
| `retry_policy` | `None` | A `RetryPolicy` shared by the whole provider fallback chain. See [RetryPolicy](#retrypolicy--providerrouter). |
| `selection` | `"ordered"` | Which provider goes first each call — `"ordered"` \| `"round_robin"` \| a callable `(providers) -> providers`. See [RetryPolicy / ProviderRouter](#retrypolicy--providerrouter). |
| `system_prompt` | `None` | Prepended as a system message on every request (via `ContextResolver`). |
| `telemetry` | `None` | Dict (`{"provider": "opentelemetry", "endpoint": ...}`), a pre-built `TracerProvider`, or `None`. See [Telemetry](#telemetry-opentelemetry). |

Exact-match cache write-back TTL is *not* a `Runtime` knob — it's controlled entirely by the
cache itself: `cache={"default_ttl": 3600}` (or the equivalent constructor arg on a pre-built
`CacheStore` instance). Earlier versions had a separate `Runtime(cache_ttl=...)` param that
always overrode `default_ttl` regardless of what the cache was configured with; it's been
removed so there's exactly one place TTL is set. See [Cache](#cache-exact-match).

`Runtime.execute()` / `Runtime.stream()` per-call kwargs: `pipeline`,
`session_id`, `user_id`, `model` (override), `system_prompt` (override —
`None` falls back to the constructor default, `""` clears it for this call),
`filters` (AST dialect for retrieval), plus any `**provider_options`
(`temperature`, `top_p`, ...) — forwarded to the provider and included in the
exact-match cache fingerprint.

`provider_metadata` is a separate kwarg, forwarded into the provider's own
request payload as Anthropic's top-level `metadata` field (`{"user_id": ...}`
for audit correlation on Anthropic's side). Don't confuse it with
`metadata=`, which is app-level (`ExecutionResult.metadata`) and never
reaches the provider. It's Anthropic-shaped, so every non-Anthropic adapter
(`GeminiProvider`, `OpenAICompatProvider` — neither has a `metadata` concept
of its own) drops it rather than forwarding it: a fallback from Anthropic to
one of these degrades (loses the audit tag) instead of failing outright. Only
the other Anthropic-family adapters (Bedrock/Vertex) forward it as-is. It's
also excluded from the exact-match cache fingerprint (unlike the rest of
`provider_options`) since it never changes what answer comes back — tagging
otherwise-identical calls with a different `user_id` doesn't defeat caching.

---

## Providers

One shared HTTP plumbing layer (`byoai/providers/base.py`):

- `DEFAULT_RETRYABLE_STATUS = {408, 409, 500, 502, 503, 504}` — the default
  retry-eligible status set; every adapter accepts `retryable_status=` to
  override it per-instance.
- `build_openai_client(...)` — shared client/header/api-key construction for
  the OpenAI-compatible family (chat + embeddings).
- `raise_for_status(...)` — shared 429→`RateLimitError` / other→`ProviderError`
  classification.

### `OpenAICompatProvider` — OpenAI, Azure, Ollama, vLLM, OpenRouter, LiteLLM, any `/chat/completions`-shaped endpoint

| Param | Default | Notes |
| --- | --- | --- |
| `model` | required | |
| `api_key` | `None` → `$OPENAI_API_KEY` | |
| `base_url` | `https://api.openai.com/v1` | Point at any compatible deployment. |
| `name` | `"openai"` | Shows up in events/errors/telemetry as `gen_ai.system`. |
| `timeout` | `60.0` | Single httpx timeout (connect+read+write+pool). For split timeouts, build your own `httpx.AsyncClient` and pass `client=`. |
| `default_headers` | `None` | Merged into every request (auth proxies, org routing). |
| `client` | `None` | Pre-built `httpx.AsyncClient` — bypasses `api_key`/`base_url`/`timeout`/`default_headers` entirely; you own its lifecycle (`Runtime.close()` only closes clients the provider built itself). |
| `chat_path` | `"/chat/completions"` | For gateways that mount the route elsewhere. |
| `retryable_status` | `None` → `DEFAULT_RETRYABLE_STATUS` | Per-instance override. |

Declarative: `llm={"provider": "openai"|"ollama"|"openrouter"|"openai_compatible"|"vllm"|"litellm", ...}`.
`ollama`/`openrouter` set sensible `base_url`/`api_key` defaults (still overridable);
`openai_compatible`/`vllm`/`litellm` require `base_url` explicitly.
`azure_openai` is handled specially in `config.py` (`endpoint`, `deployment`,
`api_version`, default `"2024-06-01"`, all overridable).

### `AnthropicProvider`

| Param | Default | Notes |
| --- | --- | --- |
| `model` | required | |
| `api_key` | `None` → `$ANTHROPIC_API_KEY` | |
| `base_url` | `https://api.anthropic.com` | |
| `name` | `"anthropic"` | |
| `timeout` | `60.0` | |
| `max_tokens` | `4096` | Default per-request cap; overridable per-call via `max_tokens=` in `execute()`. |
| `client` | `None` | Same semantics as above. |
| `api_version` | `"2023-06-01"` | The `anthropic-version` header — override when Anthropic ships a new API surface you want. |
| `default_headers` | `None` | e.g. `{"anthropic-beta": "prompt-caching-2024-07-31"}`. |
| `retryable_status` | `None` → `DEFAULT_RETRYABLE_STATUS | {529}` | 529 = Anthropic's "overloaded". |
| `messages_path` | `"/v1/messages"` | |
| `cache_system` | `False` | When `True`, a plain-string system prompt is wrapped in a `cache_control: {"type": "ephemeral"}` block so repeated calls with the same prompt hit Anthropic's server-side prompt cache. Has no effect if you already built your own system content blocks by hand (see [Provider routing & fallback](guides/providers.md#anthropic-tool-use-and-content-blocks)). |

Declarative: `llm={"provider": "anthropic", ...}`.

### `AnthropicBedrockProvider` / `AnthropicVertexProvider`

Unlike every other adapter, these depend on the `anthropic` SDK (`bedrock`/`vertex` extras) —
Bedrock's AWS SigV4 signing and Vertex's GCP OAuth service-account auth aren't reasonable to
hand-roll with raw `httpx`. Message translation and error classification (`raise_for_status()`
against the SDK's own `httpx.Response`) stay identical to every other provider.

| Param | Default | Notes |
| --- | --- | --- |
| `model` | required | |
| `name` | `"bedrock"` / `"vertex"` | |
| `max_tokens` | `4096` | |
| `client` | `None` | Pre-built `AsyncAnthropicBedrock`/`AsyncAnthropicVertex` — bypasses every other constructor param below. |
| `retryable_status` | `None` → `DEFAULT_RETRYABLE_STATUS | {529}` | |
| `cache_system` | `False` | Same as `AnthropicProvider.cache_system` above. |
| **Bedrock only** | | |
| `aws_region` | `None` → `$AWS_REGION` or `$AWS_DEFAULT_REGION` | Required (directly or via env). |
| `aws_access_key`, `aws_secret_key`, `aws_session_token`, `aws_profile` | `None` | Falls back to the standard AWS credential chain (env vars, `~/.aws`, instance/task role) when all unset. |
| **Vertex only** | | |
| `project_id` | `None` → `$ANTHROPIC_VERTEX_PROJECT_ID` or `$GOOGLE_CLOUD_PROJECT` | Required (directly or via env). |
| `region` | `None` → `$ANTHROPIC_VERTEX_REGION` or `$CLOUD_ML_REGION` | Required (directly or via env). |
| `access_token`, `credentials` | `None` | Falls back to Application Default Credentials when unset. |

Declarative: `llm={"provider": "bedrock", "model": "...", "aws_region": "..."}` /
`llm={"provider": "vertex", "model": "...", "project_id": "...", "region": "..."}`.

### `GeminiProvider`

| Param | Default | Notes |
| --- | --- | --- |
| `model` | required | |
| `api_key` | `None` → `$GEMINI_API_KEY` or `$GOOGLE_API_KEY` | |
| `base_url` | `https://generativelanguage.googleapis.com/v1beta` | |
| `name` | `"gemini"` | |
| `timeout` | `60.0` | |
| `client` | `None` | |
| `default_headers` | `None` | |
| `retryable_status` | `None` → `DEFAULT_RETRYABLE_STATUS` | |

Declarative: `llm={"provider": "gemini", ...}`.

### `OpenAICompatEmbedder` — powers `semantic_cache`/`vector_store` retrieval

| Param | Default | Notes |
| --- | --- | --- |
| `model` | required | |
| `api_key`, `base_url`, `name`, `default_headers`, `client` | same as chat adapter | `base_url` default is OpenAI's; point elsewhere for Ollama/vLLM embeddings. |
| `timeout` | `30.0` | |
| `embeddings_path` | `"/embeddings"` | |
| `retryable_status` | `None` → `DEFAULT_RETRYABLE_STATUS` | |
| `max_batch_size` | `None` (no chunking) | Transparently splits `embed_batch()` calls larger than this into multiple requests — set to your provider's cap (e.g. OpenAI: 2048) for bulk ingestion jobs. |

Declarative: `embedder={"provider": "openai"|"ollama"|"openai_compatible"|"vllm"|"litellm", ...}`.

### `RetryPolicy` / `ProviderRouter`

```python
@dataclass
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 10.0
    jitter: float = 0.25
```

One `RetryPolicy` applies to every provider in a fallback chain (retries
happen per-provider before moving to the next). Status-code retry
classification is per-adapter (`retryable_status=`, above), not on
`RetryPolicy` — that's a deliberate separation: backoff shape is
provider-agnostic, retry *eligibility* is provider-specific (Anthropic's 529
isn't the same concept as an OpenAI-compatible gateway's custom codes).

`llm={"provider": ..., "fallback": {"provider": ..., "fallback": {...}}}`
chains up to 10 tiers deep — a fixed safety rail (not a config knob) against
infinite/mistaken fallback loops; construct `providers=[...]` directly with
`ProviderRouter` if you genuinely need more.

`selection=` (also `ProviderRouter(selection=...)`) picks the order providers
are tried each call — a fallback walks whatever `selection` returned in full
on failure, so the two presets below (pure reordering) never skip a
provider; a custom callable that also filters (e.g. dropping providers it
considers unhealthy) does exclude them for that call, by its own choice:

| Value | Behavior |
| --- | --- |
| `"ordered"` (default) | Always try `providers` in the order given — identical to the router's original fixed-primary/fallback behavior. |
| `"round_robin"` | Rotates the starting provider each call, so load spreads across providers instead of always preferring the first. |
| `(providers) -> providers` | A bare callable — returns the providers to try, in order, for this call (e.g. weighted selection); may also filter, at the cost of the "nothing skipped" guarantee above. |

---

## Cache (exact-match)

### `MemoryCache`

| Param | Default | Notes |
| --- | --- | --- |
| `namespace` | `"byoai:"` | Key prefix. |
| `default_ttl` | `3600` | Seconds; explicit `ttl=` per-call overrides. `None` disables expiry — the only place TTL is set (there is no separate `Runtime`-level TTL knob). |
| `session_reader` | `None` | `{"pattern": "app:{user_id}:history"}` — read-only key-pattern mapping onto existing app state. |
| `session_data` | `None` | Dev/test stand-in for the "existing app state" `session_reader` reads. |
| `max_size` | `None` (unbounded) | Caps entry count; oldest (by last write) evicted first. |
| `url` | — | Not a `MemoryCache` param — passing it (e.g. left over from switching `provider` from `"redis"` to `"memory"`) raises `ConfigurationError` instead of being silently ignored. |

### `RedisCache` / Redis-backed everything

`make_redis_client(url, mode, sentinels, service_name, **client_kwargs)` is
the shared factory behind `RedisCache`, `RedisSemanticCache`, and
`RedisStreamQueue` — all three accept the same connection-level params:

| Param | Default | Notes |
| --- | --- | --- |
| `url` | `redis://localhost:6379` | |
| `mode` | `"standalone"` | `"standalone"` \| `"cluster"` \| `"sentinel"`. |
| `sentinels` | `None` | Required for `mode="sentinel"`: `[(host, port), ...]`. Passing it with any other `mode` raises `ConfigurationError` instead of silently connecting without it. |
| `service_name` | `None` | Required for `mode="sentinel"`. Same fail-fast as `sentinels` above. |
| `client` | `None` | Pre-built client — bypasses `url`/`mode`/`sentinels`/`service_name`/`**client_kwargs`. |
| `**client_kwargs` | — | Forwarded to redis-py: `socket_timeout`, `socket_connect_timeout`, `retry_on_timeout`, `health_check_interval`, `ssl`, `ssl_ca_certs`, etc. |

`RedisCache`-specific: `namespace="byoai:"`, `session_reader`, `default_ttl`
(same semantics as `MemoryCache`).

Declarative: `cache={"provider": "redis"|"valkey", ...}` (Valkey is
protocol-compatible — same adapter, same knobs).

---

## Semantic (intent) cache

### `MemorySemanticCache`

| Param | Default | Notes |
| --- | --- | --- |
| `capacity` | `10_000` | Ring buffer size; oldest entries evicted on overflow. |
| `ttl` | `3600` | Seconds; `None` = no expiry, `<=0` = don't store. |
| `metric` | `"cosine"` | `"cosine"` \| `"dot"` \| `"euclidean"` \| a callable `(matrix, vector) -> scores`. `"cosine"` normalizes vectors at insert/query time; the others use raw vectors. The `threshold` guidance below (0.85-0.95+) assumes `"cosine"`. |

### `RedisSemanticCache` — shared across workers, survives restarts

All `RedisCache` connection params (`url`/`mode`/`sentinels`/`service_name`/
`client`/`**client_kwargs`), plus:

| Param | Default | Notes |
| --- | --- | --- |
| `stream` | `"byoai:semcache"` | The backing Redis Stream key. |
| `capacity` | `10_000` | Both the local numpy mirror's size and the stream's `XTRIM MAXLEN` bound. |
| `ttl` | `3600` | Same semantics as `MemorySemanticCache`. |
| `metric` | `"cosine"` | Same as `MemorySemanticCache.metric` — forwarded to its local mirror, which does the actual scoring. |
| `approximate_trim` | `True` | `False` = exact `XTRIM MAXLEN` (costlier) instead of `~` approximate trimming. |

`SemanticCacheLookup` stage (what `Runtime` wires up):

| Param | Default | Notes |
| --- | --- | --- |
| `threshold` | `0.92` (`stages.DEFAULT_SEMANTIC_THRESHOLD`) | Similarity floor (assumes the default `"cosine"` metric; other metrics need a different scale — see `metric` above). Also settable via `semantic_cache={"threshold": ...}`. 0.95+ conservative, <0.85 risks off-topic hits. |

Declarative: `semantic_cache={"provider": "memory"|"redis"|"valkey", "threshold": 0.92, ...}`.

---

## Vector stores

### `PgVectorStore`

| Param | Default | Notes |
| --- | --- | --- |
| `dsn` | `None` | Required unless `pool=` given. |
| `table` | required | Existing table name (validated as a safe SQL identifier). |
| `schema_map` | `DEFAULT_SCHEMA_MAP` | `{"id": ..., "embedding": ..., "content": ..., "metadata": ...}` — zero-migration column mapping. |
| `metric` | `"cosine"` | `"cosine"` (`<=>`) \| `"l2"` (`<->`) \| `"inner_product"` (`<#>`) — must match the table's actual index operator class (`vector_cosine_ops`/`vector_l2_ops`/`vector_ip_ops`) or pgvector silently falls back to a sequential scan. `Document.score` is always "higher = more similar" regardless. |
| `pool` | `None` | Pre-built `asyncpg.Pool` — bypasses `dsn`/pool sizing/`**pool_kwargs`. |
| `min_pool_size` / `max_pool_size` | `1` / `5` | |
| `command_timeout` | `None` | Per-query timeout in seconds. |
| `**pool_kwargs` | — | Forwarded to `asyncpg.create_pool`: `server_settings={"statement_timeout": "..."}`, `ssl=...`, `max_inactive_connection_lifetime=...`, etc. |

### `QdrantVectorStore`

| Param | Default | Notes |
| --- | --- | --- |
| `url` | `http://localhost:6333` | |
| `collection` | required | |
| `api_key` | `None` | |
| `schema_map` | `{}` | `{"content": field_name, "metadata": field_name_or_None}`; `metadata: None` = whole payload. |
| `timeout` | `30.0` | |
| `client` | `None` | |
| `with_vectors` | `False` | Return embeddings on `Document.embedding`. |
| `score_threshold` | `None` | Server-side similarity floor, applied before `top_k`. |
| `search_params` | `None` | Raw Qdrant search params, e.g. `{"hnsw_ef": 128, "exact": False}` (recall/latency tradeoff). |

### `PineconeVectorStore`

| Param | Default | Notes |
| --- | --- | --- |
| `host` | required | Index data-plane host from the Pinecone console. |
| `api_key` | required | |
| `namespace` | `""` | |
| `schema_map` | `{}` | `{"content": metadata_field_name}` (Pinecone stores text in metadata). |
| `timeout` | `30.0` | |
| `client` | `None` | |
| `include_values` | `False` | Return embeddings on `Document.embedding`. |
| `sparse_vector` | `None` | Fixed `{"indices": [...], "values": [...]}` for hybrid dense+sparse search. |

Declarative: `vector_store={"provider": "pgvector"|"postgres"|"postgresql"|"qdrant"|"pinecone", ...}`.

All three vector stores share the cross-provider AST filter dialect
(`byoai.vector.filters`) — `filters={"field": {"$eq": ...}}` compiles to each
backend's native query form; see `filters.py` for the full operator set.

---

## Pipeline stages

Constructor knobs on the built-in stages (`byoai/stages.py`), reachable by
building your own `Pipeline` — the declarative `Runtime(...)` path uses their
defaults except where noted.

### `ContextResolver`

`system_prompt`, `cache`, `session_params` (callable resolving read-session
params from a `RequestContext`, overriding the default `user_id`/`session_id`
lookup), `max_history_messages=20` (`<=0` disables history injection).

### `CacheLookup` (exact-match)

`bus`, `extra_fingerprint` — an optional `(RequestContext) -> Any` hook adding
extra dimensions to the cache key (e.g. a tenant id from `ctx.state`) without
subclassing. The fingerprint always includes normalized messages, model,
pipeline name, `provider_options`, and `filters` — two requests differing
only in `temperature` or retrieval `filters` never collide on the same entry.

### `SemanticCacheLookup`

`threshold` (see above), `bus`. Embedder/store failures degrade to a cache
miss (never fail the request) — see the `cache.miss` event's `error` field.

### `VectorRetrieve`

`top_k=5`, `filters`, `bus`, and the RAG prompt wrapper:

- `format_document`: `(Document) -> str`, default `f"[{d.id}] {d.content}"`.
- `context_header`: default `"Relevant context retrieved for this request:"`.
- `insert_at`: `(RequestContext) -> int`, default just-before-the-last-message.

### `ProviderCall`

`**default_options` — provider options merged under any per-call
`provider_options` (per-call wins).

---

## Workers / queues

### `MemoryJobQueue`

`maxsize=0` (unbounded, `asyncio.Queue` semantics) — set to backpressure
`publish()` when a slow worker fleet can't keep up.

### `RedisStreamQueue`

All `RedisCache` connection params, plus:

| Param | Default | Notes |
| --- | --- | --- |
| `stream` | `"byoai:jobs"` | |
| `group` | `"byoai-workers"` | Consumer group name. |
| `consumer` | `None` → `worker-<random>` | Consumer identity within the group. |
| `result_prefix` | `"byoai:result:"` | |
| `result_ttl` | `3600` | |
| `prefetch` | `16` | Entries fetched per `XREADGROUP` round-trip. |
| `maxlen` | `None` (unbounded) | Caps the jobs stream so an idle/crashed worker fleet doesn't let it grow forever. |
| `approximate_trim` | `True` | Same tradeoff as the semantic cache's stream trim. |
| `start_id` | `"0"` | Consumer group's initial read position. `"0"` replays the whole existing stream (default, for a fresh deployment); `"$"` starts from only new entries (attaching a new worker fleet to a pre-existing, already-large stream without replaying the backlog). |

### `RuntimeWorker`

| Param | Default | Notes |
| --- | --- | --- |
| `concurrency` | `10` | |
| `shutdown_timeout` | `None` (wait forever) | Caps how long `stop()`/graceful shutdown waits for in-flight jobs to finish draining; a stuck job otherwise blocks shutdown forever. Timed-out tasks keep running in the background, just aren't awaited further. |

---

## Telemetry (OpenTelemetry)

`instrument(runtime, *, tracer_provider=None)` — attach tracing to an
existing `Runtime`; or declaratively via `Runtime(telemetry=...)`.

### `configure_otlp(...)` — builds a `TracerProvider` exporting to your collector

| Param | Default | Notes |
| --- | --- | --- |
| `endpoint` | required | |
| `service_name` | `"byoai-runtime"` | |
| `headers` | `None` | e.g. auth headers your collector requires. |
| `protocol` | `"grpc"` | `"grpc"` (port 4317) or `"http"`/`"http/protobuf"` (port 4318) — many collectors behind corporate ingress only allow HTTP. |
| `timeout` | `None` → SDK default | Exporter request timeout, seconds. |
| `compression` | `None` | `"gzip"` or `None`. |
| `resource_attributes` | `None` | Merged with `service.name`, e.g. `{"service.version": "1.2.0", "deployment.environment": "prod"}`. |
| `max_queue_size` / `schedule_delay_millis` / `max_export_batch_size` / `export_timeout_millis` | `None` → SDK defaults | `BatchSpanProcessor` tuning. Note: `max_export_batch_size` must be `<= max_queue_size`. |

Declarative: `Runtime(telemetry={"provider": "opentelemetry", "endpoint": "...", **any configure_otlp kwarg})`.
Pass a pre-built `TracerProvider` directly as `telemetry=` to skip
`configure_otlp` entirely (e.g. reusing a provider your app already
configured) — in that case `Runtime.close()` does **not** shut it down; you
own its lifecycle. A provider `Runtime` builds for you *is* shut down (final
span batch flushed) on `close()`.

---

## Transports

### FastAPI — `byoai.integrations.fastapi`

- `attach(app, runtime)` — binds `runtime.close()` to the app's shutdown hook.
- `stream_response(runtime, input, *, media_type="text/event-stream", headers=None, **execute_kwargs)` —
  `headers=None` defaults to `{"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}`
  (proxy-buffering-safe); pass `{}` to omit them, or your own dict to replace them.
- `serve_websocket(runtime, websocket)` — no extra config; drop it into your own route.

### Robyn — `byoai.integrations.robyn`

- `attach(app, runtime, *, prefix="/byoai", stream_media_type="text/event-stream", stream_headers=None)` —
  same `stream_headers=None` default-buffering-safe-headers behavior as FastAPI's `stream_response`.
- `create_app(runtime, *, prefix="/byoai", healthz_path="/healthz", **attach_kwargs)` —
  `healthz_path=None` skips registering the health route (e.g. your host app already has one).

### MCP — `byoai.integrations.mcp`

- `create_server(runtime, *, name="byoai-runtime", tool_name="execute", description=None, stream_tool_name="execute_stream", stream_description=None, **server_kwargs)` —
  registers two tools by default: `execute` (one request, one response) and
  `execute_stream` (streams token deltas as MCP progress notifications for
  clients that render them live, while still returning the same full result
  dict at the end — non-streaming-aware clients work unchanged). Pass
  `stream_tool_name=None` to register only `execute`. `**server_kwargs`
  forwarded to the MCP SDK server constructor (`instructions`, `version`,
  `debug`, `auth`, ...). Progress notifications require a live client
  session; calling `execute_stream` without one (e.g. a local `call_tool()`)
  skips reporting and still returns the correct full result.
- `attach(app, runtime, *, path="/mcp", name="byoai-runtime", **create_kwargs)` —
  mounts streamable-HTTP MCP into an existing Starlette/FastAPI app.
- `create_app(runtime, *, name="byoai-runtime", **create_kwargs)` — standalone ASGI app.
- Also works over **stdio** (`server.run_stdio_async()`) — the classic MCP
  transport for local clients like Claude Desktop; same two tools, same dialect.

All three transports (FastAPI, Robyn, MCP tool calls, queue workers) share one
payload/result/frame dialect (`byoai/transport.py`) — nothing transport-specific
to configure beyond framing.

---

## Plugins

Unknown `provider` names in any `build_*` config dict resolve through Python
entry points before raising `ConfigurationError`:

| Entry point group | Resolves for |
| --- | --- |
| `byoai.providers` | `llm={"provider": "your-name", ...}` |
| `byoai.caches` | `cache={"provider": "your-name", ...}` |
| `byoai.vector_stores` | `vector_store={"provider": "your-name", ...}` |
| `byoai.semantic_caches` | `semantic_cache={"provider": "your-name", ...}` |
| `byoai.embedders` | `embedder={"provider": "your-name", ...}` |

A plugin package registers `your-name = "your_package.module:factory"` under
the relevant group; `factory(config_dict)` returns the built instance.

---

## What's intentionally *not* configurable

- **Fallback chain depth cap (10 tiers)** — a safety rail against
  infinite/mistaken fallback loops, not a tunable. Build `providers=[...]`
  directly if you need more.
- **Cache-write-failure-must-never-fail-the-request** — `CacheLookup`,
  `SemanticCacheLookup`, and `Runtime._write_back_cache` always swallow
  `ByoAIError` from the cache layer. This is a correctness invariant, not a
  policy choice: a cache outage degrading to "no cache" is always the right
  behavior, never a per-deployment decision.

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: minor versions may include breaking changes).

## [Unreleased]

### Changed
- CI now gates on `pyright` (previously `continue-on-error`, tracked as a known gap). Fixed the
  ~60 pre-existing type errors this surfaced — mostly Optional-narrowing gaps around
  reassigned-parameter patterns in the Redis-backed adapters (`cache/redis.py`,
  `cache/semantic.py`, `workers.py`), `hasattr`-based cross-version duck typing in the
  FastAPI/MCP integrations' shutdown-hook wiring, and Optional-attribute access in test
  assertions against OpenTelemetry's SDK types. Along the way, `benchmarks/bench_json.py`'s
  cache-fingerprint benchmark was calling an instance method unbound and crashed if actually
  run — now constructs a real `CacheLookup` and measures the intended path.
- `AnthropicProvider`/`GeminiProvider`'s construction-time API-key fail-fast now only backs off
  for a caller-supplied auth header (this adapter's own, any capitalization, or a generic
  `Authorization`) in `default_headers=` — an unrelated header (e.g. a tracing header) no
  longer silently disables the check.

### Added
- `providers=` and `vector_store=` now accept a bare async function directly — no class required
  — auto-wrapped in `FunctionProvider`/`FunctionVectorStore`, matching the existing bare-callable
  pattern for `embedder=` and `Pipeline.add()`. See CONTRIBUTING.md's "bring your own function"
  design principle for which extension points get this treatment and which don't.
- `AnthropicBedrockProvider`/`AnthropicVertexProvider` (`llm={"provider": "bedrock"/"vertex", ...}`)
  — Anthropic models via AWS Bedrock or Google Vertex AI. Requires the new `bedrock`/`vertex`
  extras (depends on the `anthropic` SDK for AWS SigV4/GCP OAuth signing — the one deliberate
  exception to every other adapter's httpx-only, no-SDK design). Error classification reuses
  `raise_for_status()` against the SDK's own `httpx.Response`, so retry/fallback behaves
  identically to every other provider.
- `PipelineNotFoundError` and `AllProvidersFailedError` — PEP 8 names for the two exceptions
  that lacked the `Error` suffix. The old `PipelineNotFound`/`AllProvidersFailed` names remain
  as aliases, so existing `except` clauses keep working. `PipelineNotFoundError` also
  subclasses `LookupError`.
- `Runtime.aclose()` as an alias for `close()`, matching the async-native naming convention.
- All library-owned HTTP clients now send a `User-Agent: byoai-runtime/<version>` header
  (overridable via `default_headers`).
- `default_params=` on `OpenAICompatProvider`/`build_openai_client` — query parameters applied
  to every request (how the Azure preset now passes `api-version`).
- Library logging: cache write failures and event-handler exceptions are logged on the
  `byoai.*` loggers (a `NullHandler` is installed on the package logger; nothing is printed
  unless the application configures logging). `EventBus` without an `error_handler` logs
  swallowed subscriber exceptions at WARNING instead of discarding them.
- CI measures test coverage; the release workflow runs the full test matrix before publishing
  and pins the PyPI publish action to a commit SHA. Builds are checked with
  `twine check --strict`. A `.pre-commit-config.yaml` mirrors the ruff CI check locally.

### Changed
- The version is single-sourced from `src/byoai/_version.py`; `pyproject.toml` declares it
  `dynamic` and reads it at build time, so `byoai.__version__` can no longer drift from the
  published package version.
- The Robyn integration maps runtime errors to meaningful HTTP statuses instead of a blanket
  422: `400` malformed payload, `404` unknown pipeline, `429` provider rate limit (echoing
  `Retry-After`), `502` provider failure; other runtime errors keep `422`.
- `AnthropicProvider` and `GeminiProvider` raise `ConfigurationError` at construction when
  neither an API key (argument or environment) nor any `default_headers` are supplied, instead
  of sending a credential-less request and failing later with a provider-side 401. Passing
  `default_headers=` disables the check, for gateways that authenticate out-of-band or under a
  different header. When no key resolves, the credential header is omitted rather than sent
  blank.
- `Retry-After` headers in RFC 9110 HTTP-date form are now honored as backoff hints
  (previously only the delay-seconds form was).
- The pgvector filter compiler binds metadata field names as query parameters instead of
  splicing them into the SQL text. Generated clauses now read `meta->>$1 = $2` rather than
  `meta->>'field' = $1`; semantics are unchanged.
- OTel provider span events use GenAI semantic-convention attribute keys (`gen_ai.system`,
  `gen_ai.request.model`/`gen_ai.response.model`, `gen_ai.usage.*`, `error.message`), and
  `byoai.execute` spans carry `gen_ai.operation.name` and `gen_ai.request.model`. Dashboards
  filtering on the old ad-hoc keys (`provider`, `model`, `input_tokens`) need updating.
- `MemorySemanticCache` runs its similarity math in a worker thread once the cache exceeds
  4096 entries, so large lookups no longer stall the event loop.
- `ExecutionResult.context` and `RequestContext.documents` are now precisely typed
  (`RequestContext` / `list[Document]` instead of `Any`), and `Runtime`'s `embedder=`/
  `semantic_cache=` parameters are typed against the `Embedder`/`SemanticCacheStore`
  protocols.

### Fixed
- Streaming adapters (OpenAI-compatible, Anthropic, Gemini) now raise `ProviderError` on an
  in-band `error` event mid-stream. Previously the event was silently skipped and the
  truncated generation was delivered as a clean, successful completion.
- FastAPI's `stream_response()` turns a mid-stream runtime error into a final
  `data: {"error": ..., "done": true}` SSE event instead of tearing the connection, matching
  the Robyn integration and `transport.sse_stream`.
- The Azure OpenAI preset passes `api-version` as a real query parameter. It was previously
  baked into `base_url`, where httpx's path concatenation placed the request path after the
  query string.

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

[Unreleased]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/ravikings/byoai-runtime/releases/tag/v0.1.0a1

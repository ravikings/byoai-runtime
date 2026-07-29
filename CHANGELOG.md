# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: minor versions may include breaking changes).

## [Unreleased]

### Added
- `providers=` and `vector_store=` now accept a bare async function directly — no class required
  — auto-wrapped in `FunctionProvider`/`FunctionVectorStore`, matching the existing bare-callable
  pattern for `embedder=` and `Pipeline.add()`. See CONTRIBUTING.md's "bring your own function"
  design principle for which extension points get this treatment and which don't.

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

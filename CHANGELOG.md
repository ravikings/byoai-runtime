# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: minor versions may include breaking changes).

## [Unreleased]

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
  provider lifecycle span events, and OTLP export to an existing collector.
- `orjson`-backed hot-path JSON codec (`perf` extra) for lower-overhead request/response encoding.
- FastAPI integration (`byoai.integrations.fastapi`): `attach`, `get_runtime`, SSE `stream_response`.
- Robyn integration, WebSocket transport, and background queue workers.
- Benchmark suite for JSON encoding and runtime throughput.

[Unreleased]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/ravikings/byoai-runtime/releases/tag/v0.1.0a1

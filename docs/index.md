# ByoAI Runtime

> **Bring Your Own Infrastructure (BYOI). ByoAI Brings the Runtime.**

ByoAI Runtime is an infrastructure-agnostic AI agent engine and workflow execution layer for
Python. It connects directly to your existing Redis clusters, vector databases (pgvector,
Qdrant, Pinecone), LLMs (OpenAI-compatible, Anthropic, Gemini), and telemetry pipelines
**without requiring data migrations, vector re-indexing, database schema alterations, or vendor
lock-in.**

## Why ByoAI Runtime?

Most AI frameworks force engineering teams to adapt their database schemas, re-embed millions of
vectors, and rewrite state management logic. **ByoAI Runtime adapts to your existing stack
instead.**

- **Zero-migration schema mapping** — connect to existing vector tables/collections/indexes via a
  declarative `schema_map`; no re-indexing or data duplication.
- **Cross-provider AST filter parser** — pass one Mongo-style filter dialect; ByoAI compiles it
  to each backend's native form (pgvector JSONB, Qdrant filters, Pinecone metadata filters).
- **Non-invasive cache isolation** — runtime writes stay under an isolated `byoai:` namespace
  while a read-only `session_reader` pattern ingests chat history your app already stores.
- **Semantic (intent) caching** — serve similar, not just identical, queries from cache via
  embedding similarity, in-process or shared across workers on Redis.
- **Resilient provider routing** — retries with backoff/jitter and ordered fallback across
  providers (e.g. OpenAI → Azure OpenAI → Ollama).
- **Zero-SaaS OpenTelemetry tracing** — one span per execution, per-stage children, OTLP export
  to a collector you already run.
- **Framework-agnostic transports** — the same execution dialect over FastAPI, Robyn, WebSocket,
  and background queue workers.
- **Plugin system** — unrecognized `provider` values for `llm=`, `cache=`, `vector_store=`,
  `embedder=`, and `semantic_cache=` resolve through Python entry points, so a `pip install` adds
  a new adapter without a ByoAI code change.

## Architecture

```
                          ┌──────────────────────────┐
                          │   runtime.execute()       │
                          └─────────────┬─────────────┘
                                        │
           ┌────────────────────────────┼────────────────────────────┐
           ▼                            ▼                            ▼
┌──────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│  Cache / session      │    │  Vector store +      │    │  Provider router     │
│  (Redis / in-memory)  │    │  AST filter parser    │    │  (retry / fallback)  │
└──────────┬───────────┘    └───────────┬──────────┘    └───────────┬──────────┘
           │                            │                            │
           ▼                            ▼                            ▼
   Existing Redis DB            Existing Vector DB           LLM APIs / Inference
 (no keys overwritten)       (no vector re-indexing)       (existing API keys)
```

Start with [Getting Started](getting-started.md), or jump straight to a guide:
[FastAPI](guides/fastapi.md), [Robyn](guides/robyn.md), [Caching](guides/caching.md),
[Semantic caching](guides/semantic-cache.md), [Vector stores](guides/vector-stores.md),
[Provider routing](guides/providers.md), [Background workers](guides/workers.md),
[Telemetry](guides/telemetry.md).

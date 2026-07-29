# ByoAI Runtime

> **Bring Your Own Infrastructure (BYOI). ByoAI Brings the Runtime.**

ByoAI Runtime is an infrastructure-agnostic AI agent engine and workflow execution layer for
Python. It connects directly to your existing Redis clusters, vector databases (pgvector today;
more adapters on the roadmap), LLMs, and telemetry pipelines **without requiring data
migrations, vector re-indexing, database schema alterations, or vendor lock-in.**

## Why ByoAI Runtime?

Most AI frameworks force engineering teams to adapt their database schemas, re-embed millions of
vectors, and rewrite state management logic. **ByoAI Runtime adapts to your existing stack
instead.**

- **Zero-migration schema mapping** — connect to existing vector tables via a declarative
  `schema_map`; no re-indexing or table duplication.
- **Cross-provider AST filter parser** — pass one Mongo-style filter dialect; ByoAI compiles it
  to each backend's native form (pgvector JSONB today).
- **Non-invasive cache isolation** — runtime writes stay under an isolated `byoai:` namespace
  while a read-only `session_reader` pattern ingests chat history your app already stores.
- **Resilient provider routing** — retries with backoff/jitter and ordered fallback across
  providers (e.g. OpenAI → Azure OpenAI → Ollama).
- **Framework-agnostic transports** — the same execution dialect over FastAPI, Robyn, WebSocket,
  and background queue workers.

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
[Vector stores](guides/vector-stores.md), [Provider routing](guides/providers.md),
[Background workers](guides/workers.md).

# ByoAI Runtime (`byoai-runtime`)

> **Bring Your Own Infrastructure (BYOI). ByoAI Brings the Runtime.**

[![CI](https://github.com/ravikings/byoai-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/ravikings/byoai-runtime/actions/workflows/ci.yml)
[![PyPI version](https://badge.fury.io/py/byoai-runtime.svg)](https://badge.fury.io/py/byoai-runtime)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-brightgreen.svg)](https://www.python.org/downloads/)
[![OpenTelemetry](https://img.shields.io/badge/Observability-OpenTelemetry-purple.svg)](https://opentelemetry.io/)

ByoAI Runtime is an infrastructure-agnostic AI agent engine and workflow execution layer for Python. It connects directly to your existing Redis clusters, vector databases (pgvector, Pinecone, Qdrant), LLMs, and telemetry pipelines **without requiring data migrations, vector re-indexing, database schema alterations, or vendor lock-in.**

---

## ⚡ Why ByoAI Runtime?

Most AI frameworks force engineering teams to adapt their database schemas, re-embed millions of vectors, and rewrite state management logic. **ByoAI Runtime adapts to your existing stack instead.**

* 🔌 **Zero Vector Re-indexing (Schema Mapping):** Connect directly to existing vector tables using declarative column mapping.
* 🌲 **Cross-Provider AST Filter Parser:** Pass unified JSON filters; ByoAI translates them on the fly into native target dialects (`pgvector` JSONB, Pinecone `$eq`, Qdrant payload filters).
* 🔒 **Non-Invasive Cache Isolation:** Isolates internal runtime keys (`byoai:*`) while using pattern-mapped readers to read existing chat histories safely.
* 🛡️ **Resilient Provider Routing & Fallbacks:** Native rate-limit management, retries, and dynamic model failovers (e.g., OpenAI ➔ Azure OpenAI ➔ Ollama).
* 📊 **Zero-SaaS Telemetry:** Native OpenTelemetry (OTLP) trace emission directly to your existing Grafana, Datadog, or Honeycomb collectors.

---

## 🏗️ Architecture & Execution Loop

ByoAI Runtime executes as an unopinionated, process-level orchestrator sitting above your existing production data layers:

```
                          ┌──────────────────────────┐
                          │   runtime.execute()      │
                          └─────────────┬────────────┘
                                        │
           ┌────────────────────────────┼────────────────────────────┐
           ▼                            ▼                            ▼
┌──────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│  Redis Key Reader    │    │  AST Filter Parser   │    │ Dynamic Model Router │
│ (App Chat Ingestion) │    │ (Schema Mapping DB)  │    │ (Failover / Retry)   │
└──────────┬───────────┘    └───────────┬──────────┘    └───────────┬──────────┘
           │                            │                            │
           ▼                            ▼                            ▼
   Existing Redis DB            Existing Vector DB           LLM APIs / Inference
 (No keys overwritten)       (No vector re-indexing)      (Existing API keys)
```

---

## 🚀 Quickstart

### 1. Installation

```bash
pip install byoai-runtime
```

### 2. Hello world

```python
import asyncio
from byoai import Runtime

async def main():
    runtime = Runtime(llm={"provider": "openai", "model": "gpt-4o"})  # reads $OPENAI_API_KEY
    result = await runtime.execute("What are our enterprise SLA terms?")
    print(result.content, result.usage.total_tokens, result.cached)
    await runtime.close()

asyncio.run(main())
```

No cache, no vector store, no telemetry — just a provider. Everything else in this README is
opt-in: add `cache=`/`vector_store=`/`semantic_cache=`/`telemetry=` only for what you need, when
you need it. The next example shows all of them wired up against real infrastructure. See
[Getting Started](https://ravikings.github.io/byoai-runtime/getting-started/) for environment
variables, `system_prompt=`, and `async with Runtime(...)`.

### 3. Execution Example (Production Setup)

```python
from byoai import Runtime

# Connect to existing infrastructure without altering schemas or keys
runtime = Runtime(
    cache={
        "provider": "redis",
        "url": "redis://redis.internal:6379",
        "namespace": "byoai:",  # Isolates ByoAI state
        "session_reader": {
            "pattern": "app:users:{user_id}:chat_history", # Ingests existing history
            "format": "json"
        }
    },
    vector_store={
        "provider": "pgvector",
        "dsn": "postgresql://user:pass@localhost:5432/production_db",
        "table": "document_embeddings",
        "schema_map": {
            "id": "doc_id",
            "embedding": "embedding_v2",
            "content": "raw_text",
            "metadata": "payload_json"
        }
    },
    llm={
        "provider": "openai",
        "model": "gpt-4o",
        "fallback": {
            "provider": "azure_openai",
            "endpoint": "https://prod.openai.azure.com",
            "deployment": "gpt-4-prod",
        }
    },
    telemetry={
        "provider": "opentelemetry",
        "endpoint": "http://otel-collector.internal:4317"  # your existing collector
    },
)

# Execute through the runtime (async, from any async framework)
result = await runtime.execute(
    "What are our enterprise SLA terms?",
    user_id="usr_9912",
    filters={"department": {"$eq": "legal"}}  # Translated automatically to JSONB SQL
)

print(result.content, result.usage.total_tokens, result.cached)
```

### 4. Drop into an existing FastAPI app

```python
from fastapi import Depends, FastAPI
from byoai import Runtime
from byoai.integrations.fastapi import attach, get_runtime, stream_response

app = FastAPI()               # your existing app
attach(app, Runtime(llm={"provider": "openai", "model": "gpt-4o"}))

@app.post("/ask")
async def ask(body: dict, rt: Runtime = Depends(get_runtime)):
    result = await rt.execute(body["query"])
    return {"content": result.content, "usage": result.usage.__dict__}

@app.post("/ask/stream")      # Server-Sent Events token streaming
async def ask_stream(body: dict, rt: Runtime = Depends(get_runtime)):
    return stream_response(rt, body["query"])
```

See `examples/fastapi_app/` for a runnable app with events, caching, and fallback.

### 5. Semantic (intent) caching

Serve *similar* questions from cache — not just identical ones. One embedding
call (~15ms) replaces the whole LLM round-trip when intent matches:

```python
runtime = Runtime(
    llm={"provider": "openai", "model": "gpt-4o"},
    cache={"provider": "redis", "url": "redis://redis.internal:6379"},  # exact match
    semantic_cache={"provider": "memory", "threshold": 0.92},           # intent match
    embedder={"provider": "openai", "model": "text-embedding-3-small"},
)

await runtime.execute("What are our enterprise SLA terms?")   # LLM call (~800ms)
await runtime.execute("Tell me about our enterprise SLAs")    # intent hit (~16ms)
```

Measured ~50× faster on intent hits; lookups stay sub-millisecond to ~30k
cached answers (`benchmarks/RESULTS.md`).

For production, back the intent cache with your existing Redis so hits are
**shared across every worker/replica and survive restarts**:

```python
semantic_cache={"provider": "redis", "url": "redis://redis.internal:6379",
                "threshold": 0.92, "capacity": 10_000, "ttl": 3600}
```

Entries live in one `byoai:`-namespaced Redis stream; each worker keeps a
local numpy mirror and syncs incrementally, so similarity math never leaves
the process. Redis Cluster and Sentinel are supported everywhere Redis is
(`"mode": "cluster"` or `"mode": "sentinel"` + `sentinels`/`service_name`).

---

## 🛠️ Core Capabilities

### 1. Zero-Migration Schema Mapping
No need to run migration scripts or duplicate tables. Define a `schema_map` during initialization to bridge ByoAI to your existing table structures:

```python
vector_config = {
    "provider": "pgvector",
    "dsn": "...",
    "table": "enterprise_knowledge",
    "schema_map": {
        "id": "uuid",
        "embedding": "vector_768",
        "content": "body_text",
        "metadata": "attributes_json"
    }
}
```

### 2. AST Filter Translation
Avoid provider-specific query lock-in. Pass standard logical filter expressions and ByoAI compiles them into native query dialects:

```
                      [ AST Filter Parser ]
                                │
       ┌────────────────────────┼────────────────────────┐
       ▼                        ▼                        ▼
pgvector (SQL / JSONB)    Pinecone (JSON Dict)     Qdrant (Payload Filter)
`attributes_json->>'dept'  `{"dept": {"$eq":      `FieldCondition(key="dept",
 = 'legal'`                "legal"}}`              match=MatchValue("legal"))`
```

### 3. Non-Invasive State Management
ByoAI writes operational artifacts (semantic cache, execution traces, intent plans) under its isolated key namespace while reading existing user sessions read-only:

```python
cache_config = {
    "provider": "redis",
    "url": "redis://localhost:6379",
    "namespace": "byoai:",  # All writes go to byoai:cache:*, byoai:planner:*
    "session_reader": {
        "pattern": "session:{user_id}:messages",
        "format": "json"
    }
}
```

---

## 📊 Framework Comparison

| Architectural Criteria | LangChain / LlamaIndex | LiteLLM / Portkey | **ByoAI Runtime** |
| :--- | :--- | :--- | :--- |
| **Primary Focus** | Framework Abstractions | API Gateway / Proxy | **Unopinionated Agent Engine** |
| **Schema Migration** | ❌ Required / Enforced | N/A | **✅ Zero-Migration (Schema Mapped)** |
| **Vector Re-indexing** | ❌ Required | N/A | **✅ Direct Query over Existing Vectors** |
| **AST Metadata Translator** | ❌ Provider-specific | N/A | **✅ Cross-Provider Dialect Translation** |
| **Existing Redis Reader** | ❌ Overwrites / Requires SDK | ❌ N/A | **✅ Read-only Key Pattern Mapping** |
| **Observability** | ⚠️ Pushes Proprietary SaaS | ✅ OTel Supported | **✅ OpenTelemetry Native (OTLP)** |
| **License** | MIT | MIT / Commercial | **Apache 2.0 (Enterprise Patent Shield)** |

---

## 📦 Supported Adapter Ecosystem

### Cache & Memory
* **Redis** (Standalone, Cluster, Sentinel)
* **Valkey**
* **In-Memory** (Dev/Testing)

### Vector Databases
* **PostgreSQL + pgvector**
* **Pinecone**
* **Qdrant**
* Anything else via a `byoai.vector_stores` plugin — see [Vector stores](docs/guides/vector-stores.md#custom-adapters-via-plugins).

### LLM Providers
* **OpenAI**
* **Anthropic** (direct API, AWS Bedrock, or Google Vertex AI)
* **Azure OpenAI**
* **Google Gemini**
* **Ollama / vLLM / LiteLLM**
* **OpenRouter** / Any OpenAI-compatible REST endpoint

### Observability
* **OpenTelemetry** (Datadog, Grafana, Honeycomb, Jaeger, New Relic) — gRPC or HTTP OTLP.

### Transports
One execution, five ways in — all share the same payload/result dialect:
* **FastAPI** — `byoai.integrations.fastapi` (HTTP, SSE, WebSocket)
* **Robyn** (Rust-powered) — `byoai.integrations.robyn` (HTTP, SSE, WebSocket)
* **MCP** — `byoai.integrations.mcp`: expose the runtime as a tool any MCP client (Claude Desktop, another agent) can call, over stdio or streamable HTTP — with a streaming tool variant (live token deltas as progress notifications)
* **Queue workers** — `byoai.workers`: `RuntimeWorker` + `RedisStreamQueue`/`MemoryJobQueue`
* Or embed `Runtime` directly in any async Python process

### Configuration
Every adapter's every setting — timeouts, retry classification, connection
pooling, TTLs, capacity bounds, batch sizes, and more — is documented in
**[CONFIGURATION.md](CONFIGURATION.md)**.

---

## 🧩 Agent Context Cache

A standalone proxy, separate from `Runtime`, that sits in front of the
Anthropic API. Point Claude Code (or any Anthropic API client) at it and it
injects prompt-cache breakpoints and dedupes repeated large text blocks
within a session, cutting token spend without any client-side changes.

```bash
pip install byoai-runtime
byoai-agent-context-cache
export ANTHROPIC_BASE_URL=http://localhost:8787
```

It listens on `:8787` by default and uses Redis for session/dedup state if
`REDIS_URL` is set (falls back to an in-process store otherwise). Full env
var reference in **[CONFIGURATION.md](CONFIGURATION.md#agent-context-cache--byoai-agent-context-cache)**.

### Inspecting token-savings data

The proxy keeps a durable SQLite log of per-request usage and tokenizer-verified
benchmark samples at `BYOAI_SQLITE_PATH` (default `~/.byoai/byoai_runtime.db`).
The `/v1/stats`, `/v1/stats/benchmark`, `/v1/stats/permanent`, and
`/v1/stats/history` endpoints expose these numbers as JSON. To browse the raw
tables without writing SQL, open the file in
[`sqlite-web`](https://github.com/coleifer/sqlite-web), a small browser-based
SQLite viewer:

```bash
pip install sqlite-web
sqlite-web ~/.byoai/byoai_runtime.db   # opens a UI at http://localhost:8080
```

`sqlite-web` is an optional dev convenience, not a dependency of `byoai-runtime`.

---
# Contributing

ByoAI welcomes AI-assisted development as well as human contributions. See
[CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, checks, the AI-assisted-development policy,
and the PR process, and our [Code of Conduct](CODE_OF_CONDUCT.md). To report a vulnerability,
see [SECURITY.md](SECURITY.md) rather than opening a public issue.

---

## 📄 License & Enterprise Security

`byoai-runtime` is distributed under the **[Apache License 2.0](LICENSE)**.

* **Enterprise Safe:** Permissive license with explicit patent grants and trademark protections. Pre-approved for enterprise compliance scanners (Snyk, FOSSA, Mend).
* **Data Privacy:** Runs strictly in-process within your infrastructure. Zero data is transmitted to external servers beyond your configured model and database providers.

---

## 🌐 Community & Documentation

* **Documentation:** [ravikings.github.io/byoai-runtime](https://ravikings.github.io/byoai-runtime/)
* **GitHub Repository:** [github.com/ravikings/byoai-runtime](https://github.com/ravikings/byoai-runtime)
* **PyPI Package:** [pypi.org/project/byoai-runtime](https://pypi.org/project/byoai-runtime)
* **Changelog:** [CHANGELOG.md](CHANGELOG.md)
* **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md)
* **Security:** [SECURITY.md](SECURITY.md)
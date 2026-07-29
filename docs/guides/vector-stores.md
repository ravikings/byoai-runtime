# Vector stores & filters

pgvector requires the `pgvector` extra: `pip install "byoai-runtime[pgvector]"`. Qdrant and
Pinecone need no extra — both are built on the core `httpx` dependency.

## Zero-migration schema mapping

`byoai.vector` adapters query *existing* vector tables/collections/indexes directly — no
migrations, no re-indexing, no data duplication. A `schema_map` tells the adapter which existing
columns/fields hold each logical slot.

### pgvector

```python
runtime = Runtime(
    vector_store={
        "provider": "pgvector",  # or "postgres" / "postgresql"
        "dsn": "postgresql://user:pass@localhost:5432/production_db",
        "table": "enterprise_knowledge",
        "schema_map": {
            "id": "uuid",
            "embedding": "vector_768",
            "content": "body_text",
            "metadata": "attributes_json",
        },
    },
)
```

`byoai.vector.pgvector.PgVectorStore` also accepts `pool` (an existing `asyncpg` pool) instead of
`dsn`, `min_pool_size`/`max_pool_size`, a `command_timeout`, and arbitrary `**pool_kwargs`
forwarded to `asyncpg.create_pool()` (e.g. `server_settings={"statement_timeout": "..."}`, `ssl=...`).

### Qdrant

Reads an existing collection over Qdrant's REST API — points are never written.

```python
runtime = Runtime(
    vector_store={
        "provider": "qdrant",
        "url": "http://qdrant.internal:6333",
        "collection": "documents",
        "api_key": "...",
        "schema_map": {"content": "body_text", "metadata": None},  # None = whole payload
    },
)
```

`byoai.vector.qdrant.QdrantVectorStore` also takes `with_vectors`, a `score_threshold` (drop
results below a similarity floor before `top_k` applies), and `search_params` (HNSW knobs like
`{"hnsw_ef": 128, "exact": False}`) — Qdrant-specific, so exposed directly rather than through
the cross-provider filter dialect.

### Pinecone

Queries an existing index via its data-plane REST API — vectors are never upserted. `host` is
the index's data-plane host from the Pinecone console.

```python
runtime = Runtime(
    vector_store={
        "provider": "pinecone",
        "host": "https://my-index-abc123.svc.us-east-1-aws.pinecone.io",
        "api_key": "...",
        "namespace": "",
        "schema_map": {"content": "content"},  # Pinecone stores text in metadata
    },
)
```

`byoai.vector.pinecone.PineconeVectorStore` also takes `include_values` and a fixed
`sparse_vector` for hybrid dense+sparse search.

### Custom adapters via plugins

An unrecognized `provider` is resolved through Python entry points under the
`byoai.vector_stores` group before raising `ConfigurationError` — `pip install`ing a package that
registers a factory there adds a new vector store without a code change here. The same plugin
mechanism applies to `cache=` (`byoai.caches`), `llm=` (`byoai.providers`), `embedder=`
(`byoai.embedders`), and `semantic_cache=` (`byoai.semantic_caches`).

## Cross-provider AST filter translation

Pass one Mongo-style filter dialect; `byoai.vector.filters` parses it into a small AST, and each
adapter compiles that AST to its own native query form:

```python
{"field": "value"}                                  # implicit $eq
{"field": {"$eq" | "$ne" | "$gt" | "$gte" | "$lt" | "$lte": value}}
{"field": {"$in" | "$nin": [values]}}
{"$and": [expr, ...]}
{"$or": [expr, ...]}
{"$not": expr}
```

For pgvector, filters compile to a SQL predicate over a JSONB metadata column
(`attributes_json->>'department' = 'legal'`). Adding a new vector backend means adding one
dialect compiler against the shared AST, not another parser.

## RAG retrieval in the pipeline

`vector_store=` alone does *not* add retrieval to the default pipeline — `Runtime` only wires up
context resolution, caching, and the provider call, in that order, ending with the provider call
as the terminal stage. To retrieve documents and inject them into the prompt, add
`byoai.stages.VectorRetrieve` *before* the terminal `ProviderCall` stage, with an embedder that
turns the query into a vector (see the [Providers guide](providers.md#embeddings) for
`embedder=`). `Pipeline.add()` only appends, so insert `VectorRetrieve` by removing and
re-adding `ProviderCall` after it:

```python
from byoai import Runtime
from byoai.stages import ProviderCall, VectorRetrieve

runtime = Runtime(
    llm={"provider": "openai", "model": "gpt-4o"},
    vector_store={"provider": "pgvector", "dsn": "...", "table": "enterprise_knowledge"},
    embedder={"provider": "openai", "model": "text-embedding-3-small"},
)
runtime.pipeline.remove(ProviderCall)
runtime.pipeline.add(VectorRetrieve(runtime.vector_store, runtime.embedder, top_k=5))
runtime.pipeline.add(ProviderCall(runtime.router))

result = await runtime.execute(
    "What are our enterprise SLA terms?",
    filters={"department": {"$eq": "legal"}},  # read from ctx.state by VectorRetrieve
)
```

`filters=` passed to `runtime.execute()` only has an effect once a stage that reads it (like
`VectorRetrieve`) is on the pipeline — the default pipeline ignores it.

## Search directly

Adapters also implement `search()` for retrieval outside the pipeline entirely:

```python
docs = await runtime.vector_store.search(
    embedding=[0.01, 0.02, ...],
    top_k=5,
    filters={"department": {"$eq": "legal"}},
)
```

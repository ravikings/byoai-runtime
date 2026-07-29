# Vector stores & filters

Requires the `pgvector` extra: `pip install "byoai-runtime[pgvector]"`.

## Zero-migration schema mapping

`byoai.vector` adapters query *existing* vector tables/collections directly — no migrations, no
re-indexing, no table duplication. A `schema_map` tells the adapter which existing
columns/fields hold each logical slot (`id`, `embedding`, `content`, `metadata`):

```python
runtime = Runtime(
    vector_store={
        "provider": "pgvector",
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

Currently supported: **pgvector** (`provider: "pgvector"` / `"postgres"` / `"postgresql"`),
via `byoai.vector.pgvector.PgVectorStore`. Additional adapters (Pinecone, Qdrant, ...) are on the
roadmap — see the [filter translation](#cross-provider-ast-filter-translation) section below for
how new adapters plug in.

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

```python
result = await runtime.execute(
    "What are our enterprise SLA terms?",
    filters={"department": {"$eq": "legal"}},
)
```

For pgvector, filters compile to a SQL predicate over a JSONB metadata column
(`attributes_json->>'department' = 'legal'`). Adding a new vector backend means adding one
dialect compiler against the shared AST, not another parser.

## Search directly

Adapters also implement `search()` for retrieval outside the default pipeline:

```python
docs = await runtime.vector_store.search(
    embedding=[0.01, 0.02, ...],
    top_k=5,
    filters={"department": {"$eq": "legal"}},
)
```

from __future__ import annotations

import pytest

from byoai.errors import ConfigurationError
from byoai.vector.pgvector import PgVectorStore


class FakePool:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.last_query: str | None = None
        self.last_params: tuple = ()

    async def fetch(self, query, *params):
        self.last_query = query
        self.last_params = params
        return self.rows

    async def close(self) -> None:
        pass


async def test_default_metric_is_cosine():
    pool = FakePool()
    store = PgVectorStore(table="docs", pool=pool)
    assert store.metric == "cosine"
    await store.search([1.0, 0.0])
    query = pool.last_query
    assert query is not None
    assert "1 - (embedding <=> $1::vector) AS score" in query
    assert "ORDER BY embedding <=> $1::vector" in query


async def test_metric_l2_uses_l2_operator_and_negated_score():
    pool = FakePool()
    store = PgVectorStore(table="docs", pool=pool, metric="l2")
    await store.search([1.0, 0.0])
    query = pool.last_query
    assert query is not None
    assert "-(embedding <-> $1::vector) AS score" in query
    assert "ORDER BY embedding <-> $1::vector" in query


async def test_metric_inner_product_uses_ip_operator_and_negated_score():
    pool = FakePool()
    store = PgVectorStore(table="docs", pool=pool, metric="inner_product")
    await store.search([1.0, 0.0])
    query = pool.last_query
    assert query is not None
    assert "-(embedding <#> $1::vector) AS score" in query
    assert "ORDER BY embedding <#> $1::vector" in query


async def test_unknown_metric_rejected_at_construction():
    with pytest.raises(ConfigurationError):
        PgVectorStore(table="docs", pool=FakePool(), metric="manhattan")  # type: ignore[arg-type]


async def test_large_embedding_offloaded_to_thread_produces_same_literal():
    # Regression: building the "[v1,v2,...]" literal used to always run
    # inline, blocking the event loop for large (e.g. 1536-dim) embeddings.
    # Above _OFFLOAD_MIN_DIMS it now runs in a thread — must produce the
    # exact same query parameter either way.
    from byoai.vector.pgvector import _OFFLOAD_MIN_DIMS

    pool = FakePool()
    store = PgVectorStore(table="docs", pool=pool)
    embedding = [0.1] * _OFFLOAD_MIN_DIMS  # exactly at the offload threshold
    await store.search(embedding)
    assert pool.last_params[0] == "[" + ",".join(repr(0.1) for _ in embedding) + "]"


async def test_pool_creation_failure_redacts_dsn_from_password():
    # A bad-credentials/malformed-DSN connect failure must not leak the
    # password into the raised VectorStoreError — asyncpg's own connect/parse
    # errors can echo the DSN they were given back into the exception text.
    from byoai.errors import VectorStoreError

    class ExplodingAsyncpg:
        @staticmethod
        async def create_pool(dsn, **kwargs):
            raise ConnectionError(
                f"could not connect to server: dsn={dsn!r} (postgresql://user:hunter2@host/db)"
            )

    import sys
    import types

    fake_module = types.ModuleType("asyncpg")
    fake_module.create_pool = ExplodingAsyncpg.create_pool  # type: ignore[attr-defined]
    sys.modules["asyncpg"] = fake_module
    try:
        store = PgVectorStore(dsn="postgresql://user:hunter2@host/db", table="docs")
        with pytest.raises(VectorStoreError) as excinfo:
            await store._get_pool()
        assert "hunter2" not in str(excinfo.value)
        assert "***@host" in str(excinfo.value)
    finally:
        del sys.modules["asyncpg"]


async def test_pool_creation_failure_keeps_original_exception_type_in_message():
    # Regression: wrapping every create_pool() failure the same generic way
    # made a non-connectivity failure (e.g. a bad **pool_kwargs value raising
    # TypeError) read as "connection failed" — misleading for debugging.
    # `from exc` still keeps the original exception on __cause__ either way.
    from byoai.errors import VectorStoreError

    class ExplodingAsyncpg:
        @staticmethod
        async def create_pool(dsn, **kwargs):
            raise TypeError("create_pool() got an unexpected keyword argument 'bogus'")

    import sys
    import types

    fake_module = types.ModuleType("asyncpg")
    fake_module.create_pool = ExplodingAsyncpg.create_pool  # type: ignore[attr-defined]
    sys.modules["asyncpg"] = fake_module
    try:
        store = PgVectorStore(dsn="postgresql://x", table="docs")
        with pytest.raises(VectorStoreError) as excinfo:
            await store._get_pool()
        assert "TypeError" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, TypeError)
    finally:
        del sys.modules["asyncpg"]


async def test_mutating_metric_after_construction_takes_effect_on_next_search():
    # Regression: hoisting the SQL template into __init__ for performance
    # (see the offload test above) baked metric/table into the query once —
    # store.metric = "l2" post-construction used to silently keep scoring
    # by the original cosine query instead of switching to l2.
    pool = FakePool()
    store = PgVectorStore(table="docs", pool=pool, metric="cosine")
    store.metric = "l2"
    await store.search([1.0, 0.0])
    query = pool.last_query
    assert query is not None
    assert "-(embedding <-> $1::vector) AS score" in query
    assert "ORDER BY embedding <-> $1::vector" in query


async def test_mutating_table_after_construction_takes_effect_on_next_search():
    pool = FakePool()
    store = PgVectorStore(table="docs", pool=pool)
    store.table = "other_docs"
    await store.search([1.0, 0.0])
    query = pool.last_query
    assert query is not None
    assert "FROM other_docs" in query


async def test_mutating_metric_to_unknown_value_raises():
    store = PgVectorStore(table="docs", pool=FakePool())
    with pytest.raises(ConfigurationError):
        store.metric = "manhattan"  # type: ignore[assignment]


async def test_search_returns_documents_with_score():
    pool = FakePool(
        rows=[{"id": "1", "content": "hello", "metadata": {"a": 1}, "score": 0.5}]
    )
    store = PgVectorStore(table="docs", pool=pool)
    docs = await store.search([1.0, 0.0])
    assert len(docs) == 1
    assert docs[0].id == "1"
    assert docs[0].content == "hello"
    assert docs[0].metadata == {"a": 1}
    assert docs[0].score == 0.5

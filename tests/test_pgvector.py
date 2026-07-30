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

from __future__ import annotations

import httpx
import pytest

from byoai.errors import VectorStoreError
from byoai.vector.filters import parse, to_qdrant
from byoai.vector.pinecone import PineconeVectorStore
from byoai.vector.qdrant import QdrantVectorStore


def test_qdrant_filter_eq():
    assert to_qdrant(parse({"dept": "legal"})) == {
        "key": "dept", "match": {"value": "legal"}
    }


def test_qdrant_filter_range_and_in():
    assert to_qdrant(parse({"priority": {"$gte": 3}})) == {
        "key": "priority", "range": {"gte": 3}
    }
    assert to_qdrant(parse({"dept": {"$in": ["legal", "hr"]}})) == {
        "key": "dept", "match": {"any": ["legal", "hr"]}
    }
    assert to_qdrant(parse({"dept": {"$nin": ["spam"]}})) == {
        "must_not": [{"key": "dept", "match": {"any": ["spam"]}}]
    }


def test_qdrant_filter_logical_nesting():
    node = parse({"$or": [{"a": 1}, {"$and": [{"b": 2}, {"c": {"$ne": 3}}]}]})
    assert to_qdrant(node) == {
        "should": [
            {"key": "a", "match": {"value": 1}},
            {"must": [
                {"key": "b", "match": {"value": 2}},
                {"must_not": [{"key": "c", "match": {"value": 3}}]},
            ]},
        ]
    }


def make_qdrant(handler, **kwargs) -> QdrantVectorStore:
    client = httpx.AsyncClient(
        base_url="http://qdrant.test", transport=httpx.MockTransport(handler)
    )
    return QdrantVectorStore(collection="docs", client=client, **kwargs)


async def test_qdrant_search_maps_documents():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = request.read()
        return httpx.Response(200, json={"result": [
            {"id": 7, "score": 0.91,
             "payload": {"body_text": "SLA details", "dept": "legal"}},
        ]})

    store = make_qdrant(handler, schema_map={"content": "body_text", "metadata": None})
    docs = await store.search([0.1, 0.2], top_k=3, filters={"dept": "legal"})
    assert captured["path"] == "/collections/docs/points/search"
    assert b'"filter"' in captured["body"]
    assert docs[0].id == "7" and docs[0].content == "SLA details"
    assert docs[0].metadata["dept"] == "legal"  # whole payload as metadata
    assert docs[0].score == 0.91


async def test_qdrant_http_error_raises_vector_store_error():
    store = make_qdrant(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(VectorStoreError):
        await store.search([0.1])


def make_pinecone(handler) -> PineconeVectorStore:
    client = httpx.AsyncClient(
        base_url="https://index.test", transport=httpx.MockTransport(handler)
    )
    return PineconeVectorStore(host="https://index.test", api_key="k", client=client,
                               namespace="prod")


async def test_pinecone_query_maps_documents():
    captured = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(200, json={"matches": [
            {"id": "doc-1", "score": 0.88,
             "metadata": {"content": "the text", "dept": "legal"}},
        ]})

    store = make_pinecone(handler)
    docs = await store.search([0.1, 0.2], top_k=2, filters={"dept": {"$eq": "legal"}})
    assert b'"namespace":"prod"' in captured["body"].replace(b" ", b"")
    assert b"$eq" in captured["body"]
    assert docs[0].id == "doc-1" and docs[0].content == "the text"
    assert docs[0].score == 0.88

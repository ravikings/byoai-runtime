from __future__ import annotations

import pytest

from byoai.errors import FilterError
from byoai.vector.filters import Comparison, Logical, parse, to_pgvector_sql, to_pinecone


def test_implicit_eq():
    node = parse({"department": "legal"})
    assert node == Comparison("department", "$eq", "legal")


def test_explicit_operators():
    node = parse({"score": {"$gte": 5}})
    assert node == Comparison("score", "$gte", 5)


def test_multiple_fields_become_and():
    node = parse({"a": 1, "b": 2})
    assert isinstance(node, Logical) and node.op == "$and"
    assert len(node.children) == 2


def test_nested_logical():
    node = parse({"$or": [{"a": 1}, {"$and": [{"b": 2}, {"c": {"$ne": 3}}]}]})
    assert isinstance(node, Logical) and node.op == "$or"


def test_in_requires_list():
    with pytest.raises(FilterError):
        parse({"a": {"$in": "not-a-list"}})


def test_unknown_operator_rejected():
    with pytest.raises(FilterError):
        parse({"a": {"$regex": ".*"}})


def test_pgvector_sql_eq():
    clause, params = to_pgvector_sql(parse({"department": {"$eq": "legal"}}), "payload_json")
    assert clause == "payload_json->>$1 = $2"
    assert params == ["department", "legal"]


def test_pgvector_sql_numeric_comparison():
    clause, params = to_pgvector_sql(parse({"priority": {"$gt": 3}}), "meta")
    assert clause == "(meta->>$1)::numeric > $2"
    assert params == ["priority", 3]


def test_pgvector_sql_in():
    clause, params = to_pgvector_sql(parse({"dept": {"$in": ["legal", "hr"]}}), "meta")
    assert clause == "meta->>$1 = ANY($2)"
    assert params == ["dept", ["legal", "hr"]]


def test_pgvector_sql_logical_with_seeded_params():
    node = parse({"$and": [{"a": "x"}, {"b": {"$ne": "y"}}]})
    clause, params = to_pgvector_sql(node, "meta", params=["seed"])
    assert clause == "(meta->>$2 = $3 AND meta->>$4 != $5)"
    assert params == ["seed", "a", "x", "b", "y"]


def test_pgvector_sql_in_with_bools_uses_json_text_form():
    clause, params = to_pgvector_sql(parse({"active": {"$in": [True, False]}}), "meta")
    assert params == ["active", ["true", "false"]]  # matches JSONB ->> text extraction


def test_pgvector_sql_none_compiles_to_is_null():
    clause, _ = to_pgvector_sql(parse({"deleted_at": None}), "meta")
    assert clause == "meta->>$1 IS NULL"
    clause, _ = to_pgvector_sql(parse({"deleted_at": {"$ne": None}}), "meta")
    assert clause == "meta->>$1 IS NOT NULL"


def test_pgvector_sql_none_with_ordering_op_rejected():
    with pytest.raises(FilterError):
        to_pgvector_sql(parse({"x": {"$gt": None}}), "meta")


def test_pgvector_field_name_injection_rejected():
    with pytest.raises(FilterError):
        to_pgvector_sql(parse({"a'; DROP TABLE users; --": 1}), "meta")


def test_pinecone_roundtrip():
    assert to_pinecone(parse({"dept": "legal"})) == {"dept": {"$eq": "legal"}}
    assert to_pinecone(parse({"$or": [{"a": 1}, {"b": 2}]})) == {
        "$or": [{"a": {"$eq": 1}}, {"b": {"$eq": 2}}]
    }

"""Cross-provider AST filter parser.

Applications pass one Mongo-style filter dialect; each vector adapter compiles
it to its native form. Supported operators:

    {"field": "value"}                        — implicit $eq
    {"field": {"$eq"|"$ne"|"$gt"|"$gte"|"$lt"|"$lte": value}}
    {"field": {"$in"|"$nin": [values]}}
    {"$and": [expr, ...]}  {"$or": [expr, ...]}  {"$not": expr}

The parser first normalizes the JSON into a small AST; dialect compilers walk
the AST. Adding a provider means adding one compiler, not another parser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from .. import _json as json
from ..errors import FilterError

ComparisonOp = Literal["$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"]
_COMPARISON_OPS = {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"}
_LOGICAL_OPS = {"$and", "$or", "$not"}


@dataclass
class Comparison:
    field: str
    op: ComparisonOp
    value: Any


@dataclass
class Logical:
    op: Literal["$and", "$or", "$not"]
    children: list[FilterNode]


FilterNode = Comparison | Logical


def parse(expr: dict[str, Any]) -> FilterNode:
    if not isinstance(expr, dict) or not expr:
        raise FilterError(f"filter must be a non-empty dict, got {expr!r}")

    nodes: list[FilterNode] = []
    for key, value in expr.items():
        if key in _LOGICAL_OPS:
            if key == "$not":
                nodes.append(Logical("$not", [parse(value)]))
            else:
                if not isinstance(value, list) or not value:
                    raise FilterError(f"{key} expects a non-empty list")
                op = cast(Literal["$and", "$or"], key)
                nodes.append(Logical(op, [parse(v) for v in value]))
        elif key.startswith("$"):
            raise FilterError(f"unsupported operator {key!r}")
        elif isinstance(value, dict):
            for op, operand in value.items():
                if op not in _COMPARISON_OPS:
                    raise FilterError(f"unsupported comparison {op!r} on field {key!r}")
                if op in ("$in", "$nin") and not isinstance(operand, list):
                    raise FilterError(f"{op} on field {key!r} expects a list")
                nodes.append(Comparison(key, op, operand))  # type: ignore[arg-type]
        else:
            nodes.append(Comparison(key, "$eq", value))

    return nodes[0] if len(nodes) == 1 else Logical("$and", nodes)


# --- pgvector / JSONB dialect -------------------------------------------------

_SQL_OPS = {"$eq": "=", "$ne": "!=", "$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}


def to_pgvector_sql(
    node: FilterNode, metadata_column: str, params: list[Any] | None = None
) -> tuple[str, list[Any]]:
    """Compile to a SQL predicate over a JSONB metadata column.

    Returns ``(clause, params)`` using $N asyncpg placeholders; ``params`` may
    be pre-seeded (e.g. with the query embedding) so numbering continues.
    """
    params = params if params is not None else []

    def walk(n: FilterNode) -> str:
        if isinstance(n, Logical):
            if n.op == "$not":
                return f"NOT ({walk(n.children[0])})"
            joiner = " AND " if n.op == "$and" else " OR "
            return "(" + joiner.join(walk(c) for c in n.children) + ")"
        accessor = f"{metadata_column}->>{_sql_str(n.field)}"
        if n.op in ("$in", "$nin"):
            params.append([_to_text(v) for v in n.value])
            clause = f"{accessor} = ANY(${len(params)})"
            return f"NOT ({clause})" if n.op == "$nin" else clause
        if n.value is None:
            # ->> yields SQL NULL for JSON null or a missing key; = / != never
            # match NULL, so compile to IS [NOT] NULL instead.
            if n.op == "$eq":
                return f"{accessor} IS NULL"
            if n.op == "$ne":
                return f"{accessor} IS NOT NULL"
            raise FilterError(f"{n.op} does not support None for field {n.field!r}")
        if isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            params.append(n.value)
            return f"({accessor})::numeric {_SQL_OPS[n.op]} ${len(params)}"
        params.append(_to_text(n.value))
        return f"{accessor} {_SQL_OPS[n.op]} ${len(params)}"

    return walk(node), params


def _sql_str(field: str) -> str:
    if not field.replace("_", "").replace(".", "").isalnum():
        raise FilterError(f"invalid field name {field!r}")
    return "'" + field + "'"


def _to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --- Qdrant / Pinecone dialects (Phase 6) ------------------------------------


def to_pinecone(node: FilterNode) -> dict[str, Any]:
    """Pinecone's native dialect is already Mongo-style; re-serialize the AST."""
    if isinstance(node, Logical):
        if node.op == "$not":
            raise FilterError("Pinecone does not support $not")
        return {node.op: [to_pinecone(c) for c in node.children]}
    return {node.field: {node.op: node.value}}


def parse_json(expr: str | dict[str, Any]) -> FilterNode:
    if isinstance(expr, str):
        try:
            parsed = json.loads(expr)
        except ValueError as exc:
            raise FilterError(f"filter is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise FilterError("filter JSON must be an object")
        expr = parsed
    return parse(expr)

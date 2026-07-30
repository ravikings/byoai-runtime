"""pgvector adapter: similarity search over an existing table via schema mapping.

Read-only against your production table — SELECT only, no DDL, no writes.
Requires the ``pgvector`` extra: ``pip install byoai-runtime[pgvector]``.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import _json as json
from ..errors import ConfigurationError, VectorStoreError
from ..types import Document
from .base import DEFAULT_SCHEMA_MAP
from .filters import parse, to_pgvector_sql

_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")

PgMetric = Literal["cosine", "l2", "inner_product"]

# pgvector distance operators. ORDER BY the raw operator ascending is
# "closest first" for all three (pgvector convention), but each needs its own
# transform into Document.score so "higher = more similar" holds everywhere:
# cosine distance is in [0, 2] (score = 1 - distance); L2 and inner-product
# operators return an unbounded distance-like value (score = its negation).
# The operator must match whatever index the table was actually built with
# (vector_cosine_ops / vector_l2_ops / vector_ip_ops) — pgvector silently
# falls back to a sequential scan otherwise.
_PG_OPERATORS: dict[str, str] = {
    "cosine": "<=>",
    "l2": "<->",
    "inner_product": "<#>",
}


def _ident(name: str) -> str:
    """Validate a SQL identifier from config (table/column names)."""
    if not name or not set(name) <= _IDENT_CHARS or name[0].isdigit():
        raise ConfigurationError(f"invalid SQL identifier in schema_map/table: {name!r}")
    return name


class PgVectorStore:
    def __init__(
        self,
        *,
        dsn: str | None = None,
        table: str,
        schema_map: dict[str, str] | None = None,
        metric: PgMetric = "cosine",
        pool: Any | None = None,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
        command_timeout: float | None = None,
        **pool_kwargs: Any,
    ) -> None:
        """``metric`` must match the ``vector_*_ops`` operator class the table's index
        was actually built with: ``"cosine"`` (default, ``vector_cosine_ops``), ``"l2"``
        (``vector_l2_ops``), or ``"inner_product"`` (``vector_ip_ops``).

        ``**pool_kwargs`` (e.g. ``server_settings={"statement_timeout": "..."}``,
        ``ssl=...``, ``max_inactive_connection_lifetime=...``) are forwarded
        to ``asyncpg.create_pool`` when ``pool=`` isn't supplied directly."""
        if pool is None and dsn is None:
            raise ConfigurationError("PgVectorStore requires a dsn or an existing pool")
        if metric not in _PG_OPERATORS:
            raise ConfigurationError(
                f"unknown metric {metric!r} (expected one of {sorted(_PG_OPERATORS)})"
            )
        # asyncpg's own parameter names for the same two knobs — accepting
        # both spellings would let **pool_kwargs silently overrule
        # min_pool_size/max_pool_size with no error. (command_timeout isn't
        # in this set: it's spelled identically in both, so passing it twice
        # is a SyntaxError at the call site, not a path into **pool_kwargs.)
        collisions = {"min_size", "max_size"} & pool_kwargs.keys()
        if collisions:
            raise ConfigurationError(
                f"use min_pool_size/max_pool_size, not {sorted(collisions)} "
                "(asyncpg's own names) — passing both would silently pick one"
            )
        self._dsn = dsn
        self._pool = pool
        self._pool_opts = {
            "min_size": min_pool_size,
            "max_size": max_pool_size,
            "command_timeout": command_timeout,
            **pool_kwargs,
        }
        self.table = _ident(table)
        self.schema_map = {**DEFAULT_SCHEMA_MAP, **(schema_map or {})}
        for column in self.schema_map.values():
            _ident(column)
        self.metric = metric

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover
                raise ConfigurationError(
                    "PgVectorStore requires asyncpg: pip install 'byoai-runtime[pgvector]'"
                ) from exc
            self._pool = await asyncpg.create_pool(self._dsn, **self._pool_opts)
        return self._pool

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]:
        m = self.schema_map
        vector_literal = "[" + ",".join(repr(float(v)) for v in embedding) + "]"
        params: list[Any] = [vector_literal]
        where = ""
        if filters:
            clause, params = to_pgvector_sql(parse(filters), m["metadata"], params)
            where = f"WHERE {clause}"
        params.append(top_k)
        operator = _PG_OPERATORS[self.metric]
        distance = f"{m['embedding']} {operator} $1::vector"
        score = f"1 - ({distance})" if self.metric == "cosine" else f"-({distance})"
        query = (
            f"SELECT {m['id']} AS id, {m['content']} AS content, "
            f"{m['metadata']} AS metadata, "
            f"{score} AS score "
            f"FROM {self.table} {where} "
            f"ORDER BY {distance} "
            f"LIMIT ${len(params)}"
        )
        pool = await self._get_pool()
        try:
            rows = await pool.fetch(query, *params)
        except Exception as exc:
            raise VectorStoreError(f"pgvector search failed: {exc}") from exc
        documents = []
        for row in rows:
            metadata = row["metadata"]
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except ValueError:
                    metadata = {"raw": metadata}
            documents.append(
                Document(
                    id=str(row["id"]),
                    content=row["content"] or "",
                    metadata=metadata or {},
                    score=float(row["score"]) if row["score"] is not None else None,
                )
            )
        return documents

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

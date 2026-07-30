"""pgvector adapter: similarity search over an existing table via schema mapping.

Read-only against your production table — SELECT only, no DDL, no writes.
Requires the ``pgvector`` extra: ``pip install byoai-runtime[pgvector]``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from .. import _json as json
from ..errors import ConfigurationError, VectorStoreError
from ..types import Document
from .base import DEFAULT_SCHEMA_MAP
from .filters import parse, to_pgvector_sql

_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")

# Building the embedding literal below is pure-Python string work with no
# natural await point — long enough at large dimensions to stall the event
# loop for every other concurrent request on this worker, not just this one.
# asyncio.to_thread() itself isn't free though (~35-40us fixed overhead,
# measured), so offloading only pays off once the inline cost it avoids is
# large enough to matter more than that tax: measured inline/threaded costs
# by dimension were 256d 47/82us (1.7x), 512d 92/134us (1.5x), 1024d
# 182/218us (1.2x), 1536d 276/322us (1.2x), 3072d 568/615us (1.1x) — below
# ~1024 dims the thread-hop makes this request slower for a blocking window
# barely worth avoiding; above it the ratio flattens out and offloading
# genuinely helps other concurrent requests. Mirrors the same offload-past-
# a-threshold pattern MemorySemanticCache uses for its own per-request numpy
# work (see cache/semantic.py's _OFFLOAD_MIN_ROWS), just calibrated against
# this module's own workload instead of reused wholesale.
_OFFLOAD_MIN_DIMS = 1024


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


def _redact_dsn(text: str) -> str:
    """Strip embedded credentials (``postgresql://user:pass@host/db`` ->
    ``postgresql://***@host/db``) from a DSN or a message that might contain
    one — asyncpg's own connect/parse errors can echo the DSN it was given
    back into the exception text, and this store's own errors wrap that
    exception's ``str()`` verbatim, so a bad-credentials connection failure
    must not leak the password into whatever logs the resulting
    ``VectorStoreError``.
    """
    return re.sub(r"//[^@/\s]+@", "//***@", text)

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
        # Backing fields for the table/schema_map/metric properties below,
        # set directly here (not through the properties) to avoid each
        # setter's _rebuild_query() running against a still-partially-
        # constructed instance; _rebuild_query() runs once at the end
        # instead, once all three are actually in place.
        self._table = _ident(table)
        self._schema_map = {**DEFAULT_SCHEMA_MAP, **(schema_map or {})}
        for column in self._schema_map.values():
            _ident(column)
        if metric not in _PG_OPERATORS:
            raise ConfigurationError(
                f"unknown metric {metric!r} (expected one of {sorted(_PG_OPERATORS)})"
            )
        self._metric: PgMetric = metric
        self._rebuild_query()

    @property
    def table(self) -> str:
        return self._table

    @table.setter
    def table(self, value: str) -> None:
        self._table = _ident(value)
        self._rebuild_query()

    @property
    def schema_map(self) -> dict[str, str]:
        return self._schema_map

    @schema_map.setter
    def schema_map(self, value: dict[str, str]) -> None:
        merged = {**DEFAULT_SCHEMA_MAP, **(value or {})}
        for column in merged.values():
            _ident(column)
        self._schema_map = merged
        self._rebuild_query()

    @property
    def metric(self) -> PgMetric:
        return self._metric

    @metric.setter
    def metric(self, value: PgMetric) -> None:
        if value not in _PG_OPERATORS:
            raise ConfigurationError(
                f"unknown metric {value!r} (expected one of {sorted(_PG_OPERATORS)})"
            )
        self._metric = value
        self._rebuild_query()

    def _rebuild_query(self) -> None:
        """(Re)build the SQL fragments that depend only on table/schema_map/
        metric — all fixed for the instance's lifetime between mutations of
        those three, so a per-request search() call only ever does f-string
        work on the genuinely per-request parts (the WHERE clause and
        LIMIT). Runs once at the end of __init__ and again from each of the
        three properties' setters above, so mutating any of them after
        construction (``store.metric = "l2"``) takes effect on the next
        search() instead of silently continuing to use what __init__ saw.
        """
        m = self._schema_map
        operator = _PG_OPERATORS[self._metric]
        self._distance_expr = f"{m['embedding']} {operator} $1::vector"
        score_expr = (
            f"1 - ({self._distance_expr})"
            if self._metric == "cosine"
            else f"-({self._distance_expr})"
        )
        self._select_prefix = (
            f"SELECT {m['id']} AS id, {m['content']} AS content, "
            f"{m['metadata']} AS metadata, {score_expr} AS score FROM {self._table}"
        )

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover
                raise ConfigurationError(
                    "PgVectorStore requires asyncpg: pip install 'byoai-runtime[pgvector]'"
                ) from exc
            try:
                self._pool = await asyncpg.create_pool(self._dsn, **self._pool_opts)
            except Exception as exc:
                # create_pool() can fail for reasons that aren't a
                # connectivity problem at all (e.g. TypeError from a bad
                # **pool_kwargs value) — the exception's own type name stays
                # in the message so this doesn't read as a network failure
                # when it's actually a config bug, and `from exc` keeps the
                # original exception reachable via __cause__ either way.
                # Still always wrapped (not conditionally, by type) since
                # any exception here could in principle echo self._dsn back.
                raise VectorStoreError(
                    f"pgvector pool creation failed ({type(exc).__name__}): "
                    f"{_redact_dsn(str(exc))}"
                ) from exc
        return self._pool

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]:
        vector_literal = (
            await asyncio.to_thread(_vector_literal, embedding)
            if len(embedding) >= _OFFLOAD_MIN_DIMS
            else _vector_literal(embedding)
        )
        params: list[Any] = [vector_literal]
        where = ""
        if filters:
            clause, params = to_pgvector_sql(parse(filters), self.schema_map["metadata"], params)
            where = f"WHERE {clause}"
        params.append(top_k)
        query = (
            f"{self._select_prefix} {where} "
            f"ORDER BY {self._distance_expr} "
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

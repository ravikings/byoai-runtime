"""pgvector adapter: cosine search over an existing table via schema mapping.

Read-only against your production table — SELECT only, no DDL, no writes.
Requires the ``pgvector`` extra: ``pip install byoai-runtime[pgvector]``.
"""

from __future__ import annotations

from typing import Any

from .. import _json as json
from ..errors import ConfigurationError, VectorStoreError
from ..types import Document
from .base import DEFAULT_SCHEMA_MAP
from .filters import parse, to_pgvector_sql

_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")


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
        pool: Any | None = None,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
        command_timeout: float | None = None,
        **pool_kwargs: Any,
    ) -> None:
        """``**pool_kwargs`` (e.g. ``server_settings={"statement_timeout": "..."}``,
        ``ssl=...``, ``max_inactive_connection_lifetime=...``) are forwarded
        to ``asyncpg.create_pool`` when ``pool=`` isn't supplied directly."""
        if pool is None and dsn is None:
            raise ConfigurationError("PgVectorStore requires a dsn or an existing pool")
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
        query = (
            f"SELECT {m['id']} AS id, {m['content']} AS content, "
            f"{m['metadata']} AS metadata, "
            f"1 - ({m['embedding']} <=> $1::vector) AS score "
            f"FROM {self.table} {where} "
            f"ORDER BY {m['embedding']} <=> $1::vector "
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

from typing import TYPE_CHECKING

from .base import DEFAULT_SCHEMA_MAP, FunctionVectorStore, VectorStore
from .filters import parse, parse_json, to_pgvector_sql, to_pinecone

if TYPE_CHECKING:
    # Type checkers see this real import (no runtime cost, no asyncpg
    # dependency needed to type-check); __getattr__ below satisfies it at
    # runtime, only importing pgvector if PgVectorStore is actually accessed.
    from .pgvector import PgVectorStore

__all__ = [
    "VectorStore",
    "FunctionVectorStore",
    "DEFAULT_SCHEMA_MAP",
    "PgVectorStore",
    "parse",
    "parse_json",
    "to_pgvector_sql",
    "to_pinecone",
]


def __getattr__(name: str) -> object:
    # PgVectorStore is behind the optional `pgvector` extra; import lazily.
    if name == "PgVectorStore":
        from .pgvector import PgVectorStore

        return PgVectorStore
    raise AttributeError(name)

from .base import DEFAULT_SCHEMA_MAP, VectorStore
from .filters import parse, parse_json, to_pgvector_sql, to_pinecone

__all__ = [
    "VectorStore",
    "DEFAULT_SCHEMA_MAP",
    "PgVectorStore",
    "parse",
    "parse_json",
    "to_pgvector_sql",
    "to_pinecone",
]


def __getattr__(name: str):
    # PgVectorStore is behind the optional `pgvector` extra; import lazily.
    if name == "PgVectorStore":
        from .pgvector import PgVectorStore

        return PgVectorStore
    raise AttributeError(name)

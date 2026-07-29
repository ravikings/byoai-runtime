"""Vector store protocol with zero-migration schema mapping.

Adapters query *existing* vector tables/collections directly. A ``schema_map``
tells the adapter which existing columns/fields hold each logical slot::

    schema_map = {
        "id": "doc_id",
        "embedding": "embedding_v2",
        "content": "raw_text",
        "metadata": "payload_json",
    }

No migrations, no re-indexing, no table duplication.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..types import Document

DEFAULT_SCHEMA_MAP = {
    "id": "id",
    "embedding": "embedding",
    "content": "content",
    "metadata": "metadata",
}


@runtime_checkable
class VectorStore(Protocol):
    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]: ...

    async def close(self) -> None: ...

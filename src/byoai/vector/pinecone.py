"""Pinecone adapter: query existing indexes via the data-plane REST API
(httpx, no SDK). Read-only — vectors are never upserted.

``host`` is the index's data-plane host from the Pinecone console, e.g.
``https://my-index-abc123.svc.us-east-1-aws.pinecone.io``.
"""

from __future__ import annotations

from typing import Any

import httpx

from .._version import USER_AGENT
from ..errors import VectorStoreError
from ..types import Document
from .filters import parse, to_pinecone


class PineconeVectorStore:
    def __init__(
        self,
        *,
        host: str,
        api_key: str,
        namespace: str = "",
        schema_map: dict[str, str] | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        include_values: bool = False,
        sparse_vector: dict[str, Any] | None = None,
    ) -> None:
        self.namespace = namespace
        # metadata field holding the document text (Pinecone stores text in metadata)
        self._content_field = (schema_map or {}).get("content", "content")
        self.include_values = include_values
        # A fixed sparse component for hybrid dense+sparse search
        # ({"indices": [...], "values": [...]}); per-query sparse vectors
        # aren't supported by the fixed VectorStore.search() signature.
        self.sparse_vector = sparse_vector
        self._client = client or httpx.AsyncClient(
            base_url=host.rstrip("/"),
            headers={"Api-Key": api_key, "User-Agent": USER_AGENT},
            timeout=timeout,
        )
        self._owns_client = client is None

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]:
        body: dict[str, Any] = {
            "vector": embedding,
            "topK": top_k,
            "includeMetadata": True,
            "includeValues": self.include_values,
        }
        if self.sparse_vector is not None:
            body["sparseVector"] = self.sparse_vector
        if self.namespace:
            body["namespace"] = self.namespace
        if filters:
            body["filter"] = to_pinecone(parse(filters))
        try:
            response = await self._client.post("/query", json=body)
        except httpx.HTTPError as exc:
            raise VectorStoreError(f"pinecone query failed: {exc}") from exc
        if response.status_code >= 400:
            raise VectorStoreError(
                f"pinecone query failed: HTTP {response.status_code}: {response.text}"
            )
        documents = []
        for match in response.json().get("matches", []):
            metadata = match.get("metadata") or {}
            documents.append(
                Document(
                    id=str(match.get("id")),
                    content=str(metadata.get(self._content_field, "")),
                    metadata=metadata,
                    score=match.get("score"),
                    embedding=match.get("values") if self.include_values else None,
                )
            )
        return documents

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

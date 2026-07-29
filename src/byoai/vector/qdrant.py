"""Qdrant adapter: search existing collections via the REST API (httpx, no SDK).

Read-only against your collection — points are never written. ``schema_map``
maps ByoAI's logical slots onto your existing payload fields::

    QdrantVectorStore(
        url="http://qdrant.internal:6333",
        collection="documents",
        schema_map={"content": "body_text", "metadata": None},  # None = whole payload
    )
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import VectorStoreError
from ..types import Document
from .filters import parse, to_qdrant


class QdrantVectorStore:
    def __init__(
        self,
        *,
        url: str = "http://localhost:6333",
        collection: str,
        api_key: str | None = None,
        schema_map: dict[str, Any] | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.collection = collection
        schema_map = schema_map or {}
        # payload field holding the text; None means "no content field"
        self._content_field = schema_map.get("content", "content")
        # payload field holding metadata; None means "the whole payload"
        self._metadata_field = schema_map.get("metadata", None)
        headers = {"api-key": api_key} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=url.rstrip("/"), headers=headers, timeout=timeout
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
            "limit": top_k,
            "with_payload": True,
        }
        if filters:
            body["filter"] = to_qdrant(parse(filters))
        try:
            response = await self._client.post(
                f"/collections/{self.collection}/points/search", json=body
            )
        except httpx.HTTPError as exc:
            raise VectorStoreError(f"qdrant search failed: {exc}") from exc
        if response.status_code >= 400:
            raise VectorStoreError(
                f"qdrant search failed: HTTP {response.status_code}: {response.text}"
            )
        documents = []
        for point in response.json().get("result", []):
            payload = point.get("payload") or {}
            content = payload.get(self._content_field, "") if self._content_field else ""
            metadata = (
                payload.get(self._metadata_field) or {}
                if self._metadata_field
                else payload
            )
            documents.append(
                Document(
                    id=str(point.get("id")),
                    content=str(content),
                    metadata=metadata if isinstance(metadata, dict) else {"raw": metadata},
                    score=point.get("score"),
                )
            )
        return documents

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

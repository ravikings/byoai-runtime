"""Embeddings adapter for any OpenAI-compatible ``/embeddings`` endpoint
(OpenAI, Azure, Ollama, vLLM, ...). Powers vector retrieval and the semantic
cache; apps may also supply any ``async (str) -> list[float]`` callable of
their own instead.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import ProviderError
from .base import DEFAULT_RETRYABLE_STATUS, build_openai_client, raise_for_status


class OpenAICompatEmbedder:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        name: str = "openai",
        timeout: float = 30.0,
        default_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        embeddings_path: str = "/embeddings",
        retryable_status: frozenset[int] | set[int] | None = None,
        max_batch_size: int | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._embeddings_path = embeddings_path
        self._retryable_status = (
            frozenset(retryable_status) if retryable_status is not None
            else DEFAULT_RETRYABLE_STATUS
        )
        # e.g. OpenAI caps /embeddings at 2048 inputs per call; large batch
        # ingestion jobs are chunked transparently rather than erroring.
        self.max_batch_size = max_batch_size
        self._client, self._owns_client = build_openai_client(
            api_key=api_key, base_url=base_url, timeout=timeout,
            default_headers=default_headers, client=client,
        )

    async def __call__(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str], **options: Any) -> list[list[float]]:
        if self.max_batch_size and len(texts) > self.max_batch_size:
            import asyncio

            chunks = [
                texts[start : start + self.max_batch_size]
                for start in range(0, len(texts), self.max_batch_size)
            ]
            # Independent requests to the same endpoint — run concurrently
            # rather than serializing a bulk-ingestion job one chunk at a time.
            results = await asyncio.gather(
                *(self._embed_batch_once(chunk, **options) for chunk in chunks)
            )
            return [embedding for chunk_result in results for embedding in chunk_result]
        return await self._embed_batch_once(texts, **options)

    async def _embed_batch_once(
        self, texts: list[str], **options: Any
    ) -> list[list[float]]:
        payload = {"model": options.pop("model", self.model), "input": texts, **options}
        try:
            response = await self._client.post(self._embeddings_path, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: embeddings transport error: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc
        raise_for_status(response, provider=self.name, retryable_status=self._retryable_status)
        data = response.json().get("data") or []
        if len(data) != len(texts):
            raise ProviderError(
                f"{self.name}: embeddings response had {len(data)} vectors for "
                f"{len(texts)} inputs",
                provider=self.name,
                retryable=False,
            )
        return [item["embedding"] for item in data]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

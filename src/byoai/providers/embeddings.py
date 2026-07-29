"""Embeddings adapter for any OpenAI-compatible ``/embeddings`` endpoint
(OpenAI, Azure, Ollama, vLLM, ...). Powers vector retrieval and the semantic
cache; apps may also supply any ``async (str) -> list[float]`` callable of
their own instead.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import ProviderError
from .base import build_openai_client, raise_for_status


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
    ) -> None:
        self.name = name
        self.model = model
        self._client, self._owns_client = build_openai_client(
            api_key=api_key, base_url=base_url, timeout=timeout,
            default_headers=default_headers, client=client,
        )

    async def __call__(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str], **options: Any) -> list[list[float]]:
        payload = {"model": options.pop("model", self.model), "input": texts, **options}
        try:
            response = await self._client.post("/embeddings", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: embeddings transport error: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc
        raise_for_status(response, provider=self.name)
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

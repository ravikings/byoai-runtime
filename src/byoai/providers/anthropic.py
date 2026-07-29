"""Anthropic Messages API adapter (httpx, no SDK dependency)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import _json as json
from ..errors import ProviderError, RateLimitError
from ..types import Message, ProviderResponse, StreamChunk, Usage
from .base import parse_retry_after

_RETRYABLE_STATUS = {408, 409, 500, 502, 503, 504, 529}
_API_VERSION = "2023-06-01"


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        name: str = "anthropic",
        timeout: float = 60.0,
        max_tokens: int = 4096,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.max_tokens = max_tokens
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"x-api-key": api_key, "anthropic-version": _API_VERSION},
            timeout=timeout,
        )
        self._owns_client = client is None

    def _payload(self, messages: list[Message], options: dict[str, Any]) -> dict[str, Any]:
        system_parts = [m.content for m in messages if m.role == "system"]
        chat = [m.to_dict() for m in messages if m.role != "system"]
        payload: dict[str, Any] = {
            "model": options.pop("model", self.model),
            "max_tokens": options.pop("max_tokens", self.max_tokens),
            "messages": chat,
            **options,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except Exception:
            detail = response.text
        retry_after = parse_retry_after(response)
        if response.status_code == 429:
            raise RateLimitError(
                f"{self.name}: rate limited: {detail}",
                provider=self.name,
                retry_after=retry_after,
            )
        raise ProviderError(
            f"{self.name}: HTTP {response.status_code}: {detail}",
            provider=self.name,
            status_code=response.status_code,
            retryable=response.status_code in _RETRYABLE_STATUS,
            retry_after=retry_after,
        )

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        payload = self._payload(messages, options)
        try:
            response = await self._client.post("/v1/messages", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: transport error: {exc}", provider=self.name, retryable=True
            ) from exc
        self._raise_for_status(response)
        data = response.json()
        content = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return ProviderResponse(
            content=content,
            model=data.get("model", payload["model"]),
            provider=self.name,
            usage=Usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ),
            finish_reason=data.get("stop_reason"),
            raw=data,
        )

    async def stream(
        self, messages: list[Message], **options: Any
    ) -> AsyncIterator[StreamChunk]:
        payload = self._payload(messages, options)
        payload["stream"] = True
        model = payload["model"]
        usage = Usage()
        try:
            async with self._client.stream("POST", "/v1/messages", json=payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[len("data:") :].strip())
                    except ValueError as exc:
                        raise ProviderError(
                            f"{self.name}: malformed stream event: {exc}",
                            provider=self.name,
                            retryable=False,
                        ) from exc
                    kind = data.get("type")
                    if kind == "message_start":
                        message = data.get("message", {})
                        model = message.get("model", model)
                        usage.input_tokens = message.get("usage", {}).get("input_tokens", 0)
                    elif kind == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield StreamChunk(
                                delta=delta["text"], model=model, provider=self.name, raw=data
                            )
                    elif kind == "message_delta":
                        usage.output_tokens = data.get("usage", {}).get("output_tokens", 0)
                    elif kind == "message_stop":
                        break
                yield StreamChunk(done=True, model=model, provider=self.name, usage=usage)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: transport error: {exc}", provider=self.name, retryable=True
            ) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

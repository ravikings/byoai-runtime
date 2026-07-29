"""Adapter for any OpenAI-compatible chat-completions API.

One adapter covers OpenAI, Azure OpenAI, Ollama, vLLM, OpenRouter, LiteLLM
proxy, and any other endpoint speaking the ``/chat/completions`` dialect —
point ``base_url`` at the deployment you already run. Uses ``httpx`` directly;
no provider SDK dependency.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..errors import ProviderError, RateLimitError
from ..types import Message, ProviderResponse, StreamChunk, Usage
from .base import parse_retry_after

_RETRYABLE_STATUS = {408, 409, 500, 502, 503, 504}


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        name: str = "openai",
        timeout: float = 60.0,
        default_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.model = model
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        headers = dict(default_headers or {})
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
        )
        self._owns_client = client is None

    def _payload(self, messages: list[Message], options: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": options.pop("model", self.model),
            "messages": [m.to_dict() for m in messages],
            **options,
        }

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except Exception:
            detail = response.text
        if response.status_code == 429:
            raise RateLimitError(
                f"{self.name}: rate limited: {detail}",
                provider=self.name,
                retry_after=parse_retry_after(response),
            )
        raise ProviderError(
            f"{self.name}: HTTP {response.status_code}: {detail}",
            provider=self.name,
            status_code=response.status_code,
            retryable=response.status_code in _RETRYABLE_STATUS,
            retry_after=parse_retry_after(response),
        )

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        payload = self._payload(messages, options)
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: transport error: {exc}", provider=self.name, retryable=True
            ) from exc
        self._raise_for_status(response)
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            # e.g. Azure content filtering can return 200 with no choices.
            raise ProviderError(
                f"{self.name}: response contained no choices",
                provider=self.name,
                retryable=False,
            )
        choice = choices[0]
        usage = data.get("usage") or {}
        return ProviderResponse(
            content=choice["message"].get("content") or "",
            model=data.get("model", payload["model"]),
            provider=self.name,
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason"),
            raw=data,
        )

    async def stream(
        self, messages: list[Message], **options: Any
    ) -> AsyncIterator[StreamChunk]:
        payload = self._payload(messages, options)
        payload["stream"] = True
        payload.setdefault("stream_options", {"include_usage": True})
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=payload
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_status(response)
                model = payload["model"]
                usage: Usage | None = None
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:") :].strip()
                    if data_str == "[DONE]":
                        break
                    data = json.loads(data_str)
                    model = data.get("model", model)
                    if data.get("usage"):
                        usage = Usage(
                            input_tokens=data["usage"].get("prompt_tokens", 0),
                            output_tokens=data["usage"].get("completion_tokens", 0),
                        )
                    choices = data.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta", {}).get("content")
                        if delta:
                            yield StreamChunk(
                                delta=delta, model=model, provider=self.name, raw=data
                            )
                yield StreamChunk(done=True, model=model, provider=self.name, usage=usage)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: transport error: {exc}", provider=self.name, retryable=True
            ) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

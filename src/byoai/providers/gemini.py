"""Google Gemini adapter (generateContent API via httpx, no SDK dependency)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import _json as json
from ..errors import ProviderError
from ..types import Message, ProviderResponse, StreamChunk, Usage
from .base import DEFAULT_RETRYABLE_STATUS, parse_json_response, raise_for_status


class GeminiProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        name: str = "gemini",
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
        default_headers: dict[str, str] | None = None,
        retryable_status: frozenset[int] | set[int] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._retryable_status = (
            frozenset(retryable_status) if retryable_status is not None
            else DEFAULT_RETRYABLE_STATUS
        )
        api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY", ""
        )
        headers = {"x-goog-api-key": api_key}
        headers.update(default_headers or {})
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout,
        )
        self._owns_client = client is None

    def _payload(self, messages: list[Message], options: dict[str, Any]) -> dict[str, Any]:
        system_parts = [m.content for m in messages if m.role == "system"]
        # Gemini has two roles: assistant → "model"; user AND tool results →
        # "user" (tool outputs must reach the model, not be dropped).
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
            if m.role in ("user", "assistant", "tool")
        ]
        payload: dict[str, Any] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        options.pop("model", None)
        generation_config = {}
        for byoai_key, gemini_key in (
            ("temperature", "temperature"),
            ("max_tokens", "maxOutputTokens"),
            ("top_p", "topP"),
            ("stop", "stopSequences"),
        ):
            if byoai_key in options:
                generation_config[gemini_key] = options.pop(byoai_key)
        generation_config.update(options.pop("generation_config", {}))
        if generation_config:
            payload["generationConfig"] = generation_config
        payload.update(options)
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        raise_for_status(response, provider=self.name, retryable_status=self._retryable_status)

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(part.get("text", "") for part in parts)

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> Usage:
        meta = data.get("usageMetadata") or {}
        return Usage(
            input_tokens=meta.get("promptTokenCount", 0),
            output_tokens=meta.get("candidatesTokenCount", 0),
        )

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        model = options.pop("model", self.model)
        payload = self._payload(messages, options)
        try:
            response = await self._client.post(
                f"/models/{model}:generateContent", json=payload
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: transport error: {exc}", provider=self.name, retryable=True
            ) from exc
        self._raise_for_status(response)
        data = parse_json_response(response, provider=self.name)
        if not data.get("candidates"):
            raise ProviderError(
                f"{self.name}: response contained no candidates",
                provider=self.name,
                retryable=False,
            )
        finish = data["candidates"][0].get("finishReason")
        return ProviderResponse(
            content=self._extract_text(data),
            model=model,
            provider=self.name,
            usage=self._extract_usage(data),
            finish_reason=finish.lower() if isinstance(finish, str) else finish,
            raw=data,
        )

    async def stream(
        self, messages: list[Message], **options: Any
    ) -> AsyncIterator[StreamChunk]:
        model = options.pop("model", self.model)
        payload = self._payload(messages, options)
        usage = Usage()
        try:
            async with self._client.stream(
                "POST", f"/models/{model}:streamGenerateContent", params={"alt": "sse"},
                json=payload,
            ) as response:
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
                    if data.get("usageMetadata"):
                        usage = self._extract_usage(data)
                    delta = self._extract_text(data)
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

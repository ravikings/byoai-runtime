"""Adapter for any OpenAI-compatible chat-completions API.

One adapter covers OpenAI, Azure OpenAI, Ollama, vLLM, OpenRouter, LiteLLM
proxy, and any other endpoint speaking the ``/chat/completions`` dialect —
point ``base_url`` at the deployment you already run. Uses ``httpx`` directly;
no provider SDK dependency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import _json as json
from ..errors import ProviderError
from ..types import Message, ProviderResponse, StreamChunk, Usage
from .base import (
    DEFAULT_RETRYABLE_STATUS,
    build_openai_client,
    parse_json_response,
    raise_for_status,
)


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
        default_params: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        chat_path: str = "/chat/completions",
        retryable_status: frozenset[int] | set[int] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._chat_path = chat_path
        self._retryable_status = (
            frozenset(retryable_status) if retryable_status is not None
            else DEFAULT_RETRYABLE_STATUS
        )
        self._client, self._owns_client = build_openai_client(
            api_key=api_key, base_url=base_url, timeout=timeout,
            default_headers=default_headers, default_params=default_params,
            client=client,
        )

    def _payload(self, messages: list[Message], options: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": options.pop("model", self.model),
            "messages": [m.to_dict() for m in messages],
            **options,
        }

    def _raise_for_status(self, response: httpx.Response) -> None:
        raise_for_status(response, provider=self.name, retryable_status=self._retryable_status)

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        payload = self._payload(messages, options)
        try:
            response = await self._client.post(self._chat_path, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: transport error: {exc}", provider=self.name, retryable=True
            ) from exc
        self._raise_for_status(response)
        data = parse_json_response(response, provider=self.name)
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
                "POST", self._chat_path, json=payload
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
                    try:
                        data = json.loads(data_str)
                    except ValueError as exc:
                        raise ProviderError(
                            f"{self.name}: malformed stream event: {exc}",
                            provider=self.name,
                            retryable=False,
                        ) from exc
                    if data.get("error"):
                        # In-band failure after a 200 — must not fall through to
                        # a clean done=True as if the generation completed.
                        error = data["error"]
                        detail = (
                            error.get("message", str(error))
                            if isinstance(error, dict)
                            else str(error)
                        )
                        raise ProviderError(
                            f"{self.name}: stream error event: {detail}",
                            provider=self.name,
                            retryable=False,
                        )
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

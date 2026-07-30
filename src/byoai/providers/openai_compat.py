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
from ..types import Message, ProviderResponse, StreamChunk, ToolCallDelta, Usage
from .base import (
    DEFAULT_RETRYABLE_STATUS,
    build_openai_client,
    parse_json_response,
    raise_for_status,
    require_text_content,
    strip_provider_metadata,
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
        for m in messages:
            if m.content is None:
                # OpenAI's own wire shape for a tool-call-only assistant
                # turn: content explicitly null, the call(s) on tool_calls
                # instead — require_text_content would otherwise reject it
                # as a missing/malformed message. Any other None (a role
                # other than assistant, or an assistant message with no
                # tool_calls) has nothing legitimate to send.
                if m.role != "assistant" or not m.tool_calls:
                    raise ProviderError(
                        f"{self.name}: message content is None on a {m.role!r} message "
                        "with no tool_calls — only a live assistant tool-call turn may "
                        "omit content",
                        provider=self.name,
                        retryable=False,
                    )
                continue
            require_text_content(m, provider=self.name)
            if m.role == "tool" and not m.tool_call_id:
                # The API rejects this with a 400 either way — raising here
                # instead names the actual cause (a missing tool_call_id)
                # rather than leaving a caller to work backward from a
                # generic "invalid request" response.
                raise ProviderError(
                    f"{self.name}: a 'tool' message requires tool_call_id "
                    "(the id of the tool_calls entry it's answering)",
                    provider=self.name,
                    retryable=False,
                )
        strip_provider_metadata(options)
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
        response_id: str | None = None
        finish_reason: str | None = None
        content_parts: list[str] = []
        # index -> {"id", "type", "function": {"name", "arguments"}} so the
        # final done chunk's raw= can carry a full assembled message, mirroring
        # complete()'s raw=data. function.arguments stays the accumulated JSON
        # *string* (not re-parsed) — OpenAI's own non-streaming response keeps
        # it a string too, unlike Anthropic which parses tool_use.input.
        tool_calls: dict[int, dict[str, Any]] = {}
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
                    response_id = data.get("id", response_id)
                    if data.get("usage"):
                        usage = Usage(
                            input_tokens=data["usage"].get("prompt_tokens", 0),
                            output_tokens=data["usage"].get("completion_tokens", 0),
                        )
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        content_parts.append(text)
                        yield StreamChunk(
                            delta=text, model=model, provider=self.name, raw=data
                        )
                    # tool_calls was previously dropped entirely — a forced
                    # tool_choice call (whose only content is this) yielded
                    # zero content chunks through stream().
                    for tc in delta.get("tool_calls") or []:
                        index = tc.get("index", 0)
                        block = tool_calls.setdefault(
                            index,
                            {
                                "id": None,
                                "type": "function",
                                "function": {"name": None, "arguments": ""},
                            },
                        )
                        if tc.get("id"):
                            block["id"] = tc["id"]
                        if tc.get("type"):
                            block["type"] = tc["type"]
                        fn = tc.get("function") or {}
                        name = fn.get("name")
                        if name:
                            block["function"]["name"] = name
                        fragment = fn.get("arguments") or ""
                        block["function"]["arguments"] += fragment
                        yield StreamChunk(
                            tool_call=ToolCallDelta(
                                index=index, id=tc.get("id"), name=name, partial_json=fragment
                            ),
                            model=model,
                            provider=self.name,
                            raw=data,
                        )
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(content_parts) or None,
                }
                if tool_calls:
                    message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
                yield StreamChunk(
                    done=True,
                    model=model,
                    provider=self.name,
                    usage=usage,
                    finish_reason=finish_reason,
                    raw={
                        "id": response_id,
                        "object": "chat.completion",
                        "model": model,
                        "choices": [
                            {"index": 0, "message": message, "finish_reason": finish_reason}
                        ],
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: transport error: {exc}", provider=self.name, retryable=True
            ) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

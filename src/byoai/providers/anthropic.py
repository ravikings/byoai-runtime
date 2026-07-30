"""Anthropic Messages API adapter (httpx, no SDK dependency)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import _json as json
from .._version import USER_AGENT
from ..errors import ConfigurationError, ProviderError, RateLimitError
from ..types import Message, ProviderResponse, StreamChunk, ToolCallDelta, Usage
from .base import (
    DEFAULT_RETRYABLE_STATUS,
    build_anthropic_system_field,
    has_auth_header,
    parse_json_response,
    raise_for_status,
)

# 529 = Anthropic's "overloaded" status, retryable like a 503.
DEFAULT_RETRYABLE_STATUS_ANTHROPIC = DEFAULT_RETRYABLE_STATUS | {529}
DEFAULT_API_VERSION = "2023-06-01"


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
        api_version: str = DEFAULT_API_VERSION,
        default_headers: dict[str, str] | None = None,
        retryable_status: frozenset[int] | set[int] | None = None,
        messages_path: str = "/v1/messages",
        cache_system: bool = False,
    ) -> None:
        self.name = name
        self.model = model
        self.max_tokens = max_tokens
        self._messages_path = messages_path
        # When True, a plain-string system prompt is wrapped in Anthropic's
        # cache_control ephemeral block so repeated calls with the same
        # prompt hit the server-side prompt cache. No effect on a system
        # message whose content is already a list of content blocks (the
        # caller built their own — see build_anthropic_system_field).
        self.cache_system = cache_system
        self._retryable_status = (
            frozenset(retryable_status) if retryable_status is not None
            else DEFAULT_RETRYABLE_STATUS_ANTHROPIC
        )
        self._owns_client = client is None
        if client is None:
            api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not api_key and not has_auth_header(default_headers, "x-api-key", "authorization"):
                # Fail fast at construction rather than sending a credential-less
                # request and surfacing a confusing 401 at request time. A
                # default_headers= carrying its own auth header (this adapter's,
                # however capitalized, or a generic Authorization) disables this
                # check: a gateway may authenticate under a different scheme —
                # but an unrelated header (e.g. a tracing header) must not.
                raise ConfigurationError(
                    "AnthropicProvider needs an API key: pass api_key= or set "
                    "the ANTHROPIC_API_KEY environment variable (or supply your "
                    "own auth via default_headers=)"
                )
            headers = {"anthropic-version": api_version, "User-Agent": USER_AGENT}
            if api_key:
                headers["x-api-key"] = api_key
            headers.update(default_headers or {})
            client = httpx.AsyncClient(
                base_url=base_url.rstrip("/"), headers=headers, timeout=timeout,
            )
        self._client = client

    def _payload(self, messages: list[Message], options: dict[str, Any]) -> dict[str, Any]:
        chat = [m.to_dict() for m in messages if m.role != "system"]
        payload: dict[str, Any] = {
            "model": options.pop("model", self.model),
            "max_tokens": options.pop("max_tokens", self.max_tokens),
            "messages": chat,
            **options,
        }
        system = build_anthropic_system_field(messages, cache_system=self.cache_system)
        if system is not None:
            payload["system"] = system
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        raise_for_status(response, provider=self.name, retryable_status=self._retryable_status)

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        payload = self._payload(messages, options)
        try:
            response = await self._client.post(self._messages_path, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: transport error: {exc}", provider=self.name, retryable=True
            ) from exc
        self._raise_for_status(response)
        data = parse_json_response(response, provider=self.name)
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
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
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
        finish_reason: str | None = None
        response_id: str | None = None
        # Per-content-block accumulator (index -> block dict), so the final
        # done chunk's raw= can carry a full response shape — id, every
        # content block (text and tool_use, the latter's input fully
        # re-parsed from its accumulated partial_json) — mirroring complete()'s
        # raw=data instead of leaving streaming callers with nothing to hang
        # a tool call's final arguments or an audit log's response id on.
        blocks: dict[int, dict[str, Any]] = {}
        try:
            async with self._client.stream("POST", self._messages_path, json=payload) as response:
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
                        response_id = message.get("id")
                        message_usage = message.get("usage", {})
                        usage.input_tokens = message_usage.get("input_tokens", 0)
                        usage.cache_read_tokens = message_usage.get("cache_read_input_tokens", 0)
                        usage.cache_creation_tokens = message_usage.get(
                            "cache_creation_input_tokens", 0
                        )
                    elif kind == "content_block_start":
                        index = data.get("index", 0)
                        block = data.get("content_block") or {}
                        blocks[index] = dict(block)
                        if block.get("type") == "text":
                            blocks[index]["_text_parts"] = []
                        elif block.get("type") == "thinking":
                            blocks[index]["_thinking_parts"] = []
                            blocks[index]["_signature_parts"] = []
                        elif block.get("type") == "tool_use":
                            blocks[index]["_partial_json"] = ""
                            yield StreamChunk(
                                tool_call=ToolCallDelta(
                                    index=index, id=block.get("id"), name=block.get("name")
                                ),
                                model=model,
                                provider=self.name,
                                raw=data,
                            )
                    elif kind == "content_block_delta":
                        index = data.get("index", 0)
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            text = delta["text"]
                            block = blocks.setdefault(index, {"type": "text", "_text_parts": []})
                            block["_text_parts"].append(text)
                            yield StreamChunk(
                                delta=text, model=model, provider=self.name, raw=data
                            )
                        elif delta.get("type") == "input_json_delta":
                            # input_json_delta was previously dropped entirely — a
                            # forced tool_choice call (this content type is the
                            # *only* content a tool-only turn ever produces) yielded
                            # zero content chunks through stream().
                            fragment = delta.get("partial_json", "")
                            block = blocks.setdefault(
                                index, {"type": "tool_use", "_partial_json": ""}
                            )
                            block["_partial_json"] = block.get("_partial_json", "") + fragment
                            yield StreamChunk(
                                tool_call=ToolCallDelta(index=index, partial_json=fragment),
                                model=model,
                                provider=self.name,
                                raw=data,
                            )
                        elif delta.get("type") in ("thinking_delta", "signature_delta"):
                            # Extended-thinking content — accumulated into raw["content"]
                            # only, never surfaced via StreamChunk.delta: unlike text_delta,
                            # this isn't the visible answer, and Anthropic requires the
                            # thinking block sent back unmodified on the next turn (when
                            # interleaved thinking + tool_use is in play), so silently
                            # reconstructing it empty would corrupt that round-trip.
                            is_thinking = delta["type"] == "thinking_delta"
                            key = "_thinking_parts" if is_thinking else "_signature_parts"
                            field = "thinking" if is_thinking else "signature"
                            block = blocks.setdefault(
                                index,
                                {"type": "thinking", "_thinking_parts": [], "_signature_parts": []},
                            )
                            block.setdefault(key, []).append(delta.get(field, ""))
                    elif kind == "message_delta":
                        usage.output_tokens = data.get("usage", {}).get("output_tokens", 0)
                        finish_reason = data.get("delta", {}).get("stop_reason") or finish_reason
                    elif kind == "error":
                        # In-band failure after a 200 (overloaded_error, mid-stream
                        # rate limit, ...) — must not fall through to a clean
                        # done=True as if the generation completed.
                        error = data.get("error") or {}
                        error_type = error.get("type", "error")
                        detail = error.get("message", "")
                        if error_type == "rate_limit_error":
                            raise RateLimitError(
                                f"{self.name}: rate limited mid-stream: {detail}",
                                provider=self.name,
                            )
                        raise ProviderError(
                            f"{self.name}: stream error event ({error_type}): {detail}",
                            provider=self.name,
                            retryable=error_type == "overloaded_error",
                        )
                    elif kind == "message_stop":
                        break
                content: list[dict[str, Any]] = []
                for index in sorted(blocks):
                    block = dict(blocks[index])
                    text_parts = block.pop("_text_parts", None)
                    if text_parts is not None:
                        block["text"] = "".join(text_parts)
                    thinking_parts = block.pop("_thinking_parts", None)
                    signature_parts = block.pop("_signature_parts", None)
                    if thinking_parts is not None:
                        block["thinking"] = "".join(thinking_parts)
                    if signature_parts is not None:
                        block["signature"] = "".join(signature_parts)
                    partial_json = block.pop("_partial_json", None)
                    if partial_json is not None:
                        try:
                            block["input"] = json.loads(partial_json) if partial_json else {}
                        except ValueError as exc:
                            # A caller feeding raw["content"] straight into a tool
                            # executor expects input to always be a dict — input=None
                            # would surface as a confusing AttributeError/TypeError
                            # there instead of a clear error at the point of failure.
                            raise ProviderError(
                                f"{self.name}: malformed tool_use input JSON: {exc}",
                                provider=self.name,
                                retryable=False,
                            ) from exc
                    content.append(block)
                yield StreamChunk(
                    done=True,
                    model=model,
                    provider=self.name,
                    usage=usage,
                    finish_reason=finish_reason,
                    raw={
                        "id": response_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": content,
                        "stop_reason": finish_reason,
                        "usage": {
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cache_read_input_tokens": usage.cache_read_tokens,
                            "cache_creation_input_tokens": usage.cache_creation_tokens,
                        },
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: transport error: {exc}", provider=self.name, retryable=True
            ) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

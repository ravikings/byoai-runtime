"""LLM provider protocol.

An adapter turns ByoAI's normalized request (messages + options) into one
provider's wire format and back. Adapters must raise :class:`ProviderError`
(or subclasses) for all failures so the router can make retry/fallback
decisions without knowing provider specifics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

import httpx

from ..types import Message, ProviderResponse, StreamChunk


def parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a Retry-After header as delay-seconds; None for absent or the
    RFC 7231 HTTP-date form (which we treat as 'no usable delay')."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(
        self, messages: list[Message], **options: Any
    ) -> ProviderResponse: ...

    def stream(
        self, messages: list[Message], **options: Any
    ) -> AsyncIterator[StreamChunk]: ...

    async def close(self) -> None: ...

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


DEFAULT_RETRYABLE_STATUS = frozenset({408, 409, 500, 502, 503, 504})


def raise_for_status(
    response: httpx.Response,
    *,
    provider: str,
    retryable_status: frozenset[int] | set[int] = DEFAULT_RETRYABLE_STATUS,
) -> None:
    """Shared HTTP error normalization: 429 → RateLimitError, everything else
    → ProviderError with retryability derived from the status set. Used by all
    httpx-based adapters so classification can't drift between them."""
    from ..errors import ProviderError, RateLimitError

    if response.status_code < 400:
        return
    try:
        detail = response.json().get("error", {}).get("message", response.text)
    except Exception:
        detail = response.text
    if response.status_code == 429:
        raise RateLimitError(
            f"{provider}: rate limited: {detail}",
            provider=provider,
            retry_after=parse_retry_after(response),
        )
    raise ProviderError(
        f"{provider}: HTTP {response.status_code}: {detail}",
        provider=provider,
        status_code=response.status_code,
        retryable=response.status_code in retryable_status,
        retry_after=parse_retry_after(response),
    )


def parse_json_response(response: httpx.Response, *, provider: str) -> Any:
    """Parse a response body as JSON, wrapping a malformed body (e.g. a 200
    from a misconfigured gateway that isn't actually JSON) as a clean
    ProviderError instead of letting a raw JSONDecodeError escape the adapter
    — every adapter failure must be a ProviderError so the router can act on it."""
    from ..errors import ProviderError

    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(
            f"{provider}: malformed response body: {exc}",
            provider=provider,
            retryable=False,
        ) from exc


def build_openai_client(
    *,
    api_key: str | None,
    base_url: str,
    timeout: float,
    default_headers: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
    env_var: str = "OPENAI_API_KEY",
) -> tuple[httpx.AsyncClient, bool]:
    """Shared httpx client construction for OpenAI-compatible endpoints.

    Returns ``(client, owns_client)`` — adapters close only clients they own.
    """
    import os

    api_key = api_key or os.environ.get(env_var)
    headers = dict(default_headers or {})
    if api_key:
        headers.setdefault("Authorization", f"Bearer {api_key}")
    if client is not None:
        return client, False
    return (
        httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout),
        True,
    )


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

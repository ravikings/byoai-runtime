"""LLM provider protocol.

An adapter turns ByoAI's normalized request (messages + options) into one
provider's wire format and back. Adapters must raise :class:`ProviderError`
(or subclasses) for all failures so the router can make retry/fallback
decisions without knowing provider specifics.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from .._version import USER_AGENT
from ..types import Message, ProviderResponse, StreamChunk

# The adapter-author toolkit: the protocol itself, the function wrapper, and
# the shared HTTP-normalization helpers every httpx-based adapter uses.
__all__ = [
    "LLMProvider",
    "FunctionProvider",
    "DEFAULT_RETRYABLE_STATUS",
    "parse_retry_after",
    "raise_for_status",
    "parse_json_response",
    "build_openai_client",
    "has_auth_header",
    "build_anthropic_system_field",
    "require_text_content",
    "strip_provider_metadata",
]


def has_auth_header(headers: dict[str, str] | None, *names: str) -> bool:
    """True if any of ``names`` appears as a key in ``headers``, ignoring case
    (HTTP header names are case-insensitive, but a plain ``dict`` isn't).

    Used by adapters' construction-time API-key fail-fast check: any *other*
    header (e.g. a tracing header) shouldn't silently disable it, but a
    caller-supplied auth header — this adapter's own (however capitalized) or
    a generic ``Authorization`` — means they're deliberately handling auth
    themselves (a gateway under mTLS/network policy, a different scheme).
    """
    if not headers:
        return False
    lowered = {key.lower() for key in headers}
    return any(name.lower() in lowered for name in names)


def parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a Retry-After header as delay-seconds. Both RFC 9110 forms are
    accepted: delay-seconds and HTTP-date (converted to a delay from now)."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # RFC 9110's obsolete asctime form carries no zone; it means UTC, but a
        # naive datetime's .timestamp() would assume the local system zone.
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - time.time())


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


def parse_json_response(response: httpx.Response, *, provider: str) -> dict[str, Any]:
    """Parse a response body as a JSON object, wrapping a malformed body (not
    JSON at all, or valid JSON that isn't an object — a bare list or null,
    e.g. from a misconfigured gateway) as a clean ProviderError instead of
    letting a raw JSONDecodeError/AttributeError escape the adapter — every
    adapter failure must be a ProviderError so the router can act on it."""
    from ..errors import ProviderError

    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"{provider}: malformed response body: {exc}",
            provider=provider,
            retryable=False,
        ) from exc
    if not isinstance(data, dict):
        raise ProviderError(
            f"{provider}: response body was valid JSON but not an object "
            f"(got {type(data).__name__})",
            provider=provider,
            retryable=False,
        )
    return data


def require_text_content(message: Message, *, provider: str) -> str:
    """``Message.content`` also accepts a list of provider content blocks
    (Anthropic's tool_use/tool_result/image blocks) — only the Anthropic
    adapters handle that today. Every other adapter needs plain text; call
    this instead of using ``message.content`` directly so a stray
    list-valued message fails with a clear ``ProviderError`` rather than
    being forwarded as a malformed (Anthropic-shaped, not provider-shaped)
    request body.

    Raises ``ProviderError`` (non-retryable), not ``ConfigurationError``: a
    prior turn's Anthropic tool_use/tool_result content landing on a message
    that later reaches a non-Anthropic fallback provider is a per-request,
    per-provider incompatibility — exactly what ``ProviderRouter`` falls
    through on — not a setup-time misconfiguration. ``ConfigurationError``
    here would escape the router's ``except ProviderError`` and hard-fail the
    whole request instead of trying the next provider in the chain.
    """
    if not isinstance(message.content, str):
        from ..errors import ProviderError

        raise ProviderError(
            f"{provider} requires plain-text message content, got a list of "
            f"content blocks on a {message.role!r} message — content blocks "
            "are only supported by the Anthropic adapters",
            provider=provider,
            retryable=False,
        )
    return message.content


def strip_provider_metadata(options: dict[str, Any]) -> None:
    """Drop ``metadata=`` (``Runtime.execute()``'s ``provider_metadata=``,
    Anthropic's own top-level request field for audit correlation) in place.

    Every non-Anthropic adapter needs this: Gemini's API rejects unrecognized
    fields outright, and this one also fronts several strict-schema OpenAI-
    compatible backends (Azure, vLLM, ...) that may 400 on it too — call this
    instead of blindly forwarding `options` so a fallback chain degrading to
    one of these providers after `provider_metadata=` doesn't fail outright.
    """
    options.pop("metadata", None)


def build_anthropic_system_field(
    messages: list[Message], *, cache_system: bool
) -> str | list[dict[str, Any]] | None:
    """Shared by the httpx-based ``AnthropicProvider`` and the SDK-based
    Bedrock/Vertex adapters — both build Anthropic's ``system`` request field
    from the same list of ``Message(role="system", ...)`` entries.

    ``content`` on those messages is either plain text (the common case) or
    already a list of Anthropic content blocks (a caller who hand-built
    ``cache_control`` blocks themselves). Mixing the two across multiple
    system messages has no sane default, so it's rejected outright rather
    than guessed at.
    """
    from ..errors import ConfigurationError

    system_msgs = [m.content for m in messages if m.role == "system"]
    if not system_msgs:
        return None
    has_list = any(isinstance(c, list) for c in system_msgs)
    has_str = any(isinstance(c, str) for c in system_msgs)
    if has_list and has_str:
        raise ConfigurationError(
            "cannot mix plain-text and pre-built content-block system messages "
            "in one request — make every system message's content either a "
            "str or a list[dict] of content blocks, not a mix"
        )
    if has_list:
        # cache_system is ignored here: the caller already built their own
        # blocks (presumably including any cache_control they wanted), so
        # this flag — which only auto-wraps a plain-string prompt — has
        # nothing to add.
        blocks: list[dict[str, Any]] = []
        for c in system_msgs:
            blocks.extend(c)  # type: ignore[arg-type]
        # An empty blocks list (every system message's content was itself an
        # empty list) must not become payload["system"] = [] — Anthropic's API
        # rejects an empty system array; None omits the field entirely, same
        # as "no system prompt at all".
        return blocks or None
    joined = "\n\n".join(system_msgs)  # type: ignore[arg-type]
    if cache_system:
        return [{"type": "text", "text": joined, "cache_control": {"type": "ephemeral"}}]
    return joined


def build_openai_client(
    *,
    api_key: str | None,
    base_url: str,
    timeout: float,
    default_headers: dict[str, str] | None = None,
    default_params: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
    env_var: str = "OPENAI_API_KEY",
) -> tuple[httpx.AsyncClient, bool]:
    """Shared httpx client construction for OpenAI-compatible endpoints.

    ``default_params`` become query parameters on every request (e.g. Azure's
    ``api-version``) — query strings never belong inside ``base_url``, where
    httpx's path concatenation would mangle them.
    Returns ``(client, owns_client)`` — adapters close only clients they own.
    """
    import os

    api_key = api_key or os.environ.get(env_var)
    headers = dict(default_headers or {})
    if api_key:
        headers.setdefault("Authorization", f"Bearer {api_key}")
    headers.setdefault("User-Agent", USER_AGENT)
    if client is not None:
        return client, False
    return (
        httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            params=default_params,
            timeout=timeout,
        ),
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


class FunctionProvider:
    """Adapts a bare async function into an :class:`LLMProvider` — for a
    custom or gateway-wrapped backend that doesn't fit a declarative
    ``llm={...}`` config. Mirrors ``Pipeline``'s ``FunctionStage`` and the
    ``embedder=`` callable pattern: give the runtime one function, not a class.

        async def my_gateway(messages: list[Message], **options) -> str:
            response = await my_existing_client.create(...)
            return response.text

        Runtime(providers=[my_gateway])  # auto-wrapped — no class needed

    ``fn`` may return a plain ``str`` (model/usage/finish_reason default to
    empty/zero) or a full :class:`ProviderResponse` when you want those
    tracked. ``providers=[...]`` passed to :class:`ProviderRouter` auto-wraps
    any bare callable this way; you rarely need to construct this directly.
    """

    def __init__(
        self,
        fn: Callable[..., Awaitable[str | ProviderResponse]],
        *,
        name: str | None = None,
        model: str = "",
        stream_fn: Callable[..., AsyncIterator[str | StreamChunk]] | None = None,
    ) -> None:
        self._fn = fn
        self.name = name or getattr(fn, "__name__", "function_provider")
        self.model = model
        self._stream_fn = stream_fn

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        from ..errors import ProviderError

        try:
            result = await self._fn(messages, **options)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - any wrapped-function failure must
            # become a ProviderError, same as the built-in adapters wrap httpx.HTTPError,
            # so ProviderRouter's `except ProviderError` retry/fallback actually applies.
            # Not retryable by default (unlike a transport error) — we don't know the
            # cause, and most causes (a bug, bad baked-in credentials) aren't transient;
            # a function that DOES want retry should raise ProviderError(retryable=True)
            # itself, which the `except ProviderError: raise` above passes through untouched.
            raise ProviderError(
                f"{self.name}: {exc}", provider=self.name, retryable=False
            ) from exc
        if isinstance(result, ProviderResponse):
            return result
        if not isinstance(result, str):
            # A wrapped function that returns None (e.g. a branch falling through
            # without an explicit return) must not be silently stringified into the
            # literal text "None" and delivered as if it were a real answer.
            raise ProviderError(
                f"{self.name}: expected a str or ProviderResponse, got "
                f"{type(result).__name__}",
                provider=self.name,
                retryable=False,
            )
        return ProviderResponse(
            content=result, model=options.get("model", self.model), provider=self.name
        )

    async def stream(
        self, messages: list[Message], **options: Any
    ) -> AsyncIterator[StreamChunk]:
        from ..errors import ProviderError

        if self._stream_fn is None:
            # A ProviderError (not NotImplementedError) so ProviderRouter.stream()'s
            # `except ProviderError` can fall back to the next provider instead of
            # this crashing the whole request.
            raise ProviderError(
                f"{self.name}: this FunctionProvider has no stream_fn — pass one to "
                f"FunctionProvider(fn, stream_fn=...) to support runtime.stream()",
                provider=self.name,
                retryable=False,
            )
        model = options.get("model", self.model)
        # If the caller yields raw StreamChunks, trust them to end with one
        # marked done=True (full control, e.g. to attach usage). If they only
        # yield plain str deltas, synthesize the trailing done chunk for them.
        saw_done = False
        try:
            async for item in self._stream_fn(messages, **options):
                if isinstance(item, StreamChunk):
                    saw_done = saw_done or item.done
                    yield item
                elif isinstance(item, str):
                    yield StreamChunk(delta=item, model=model, provider=self.name)
                else:
                    # Same "don't silently stringify a bug" rule as complete().
                    raise ProviderError(
                        f"{self.name}: stream_fn yielded {type(item).__name__}, expected "
                        f"str or StreamChunk",
                        provider=self.name,
                        retryable=False,
                    )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - see complete()
            raise ProviderError(
                f"{self.name}: {exc}", provider=self.name, retryable=False
            ) from exc
        if not saw_done:
            yield StreamChunk(done=True, model=model, provider=self.name)

    async def close(self) -> None:
        pass  # the wrapped function owns whatever client/resources it uses

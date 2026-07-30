"""Resilient provider routing: retries with backoff, then ordered fallback.

The router tries the primary provider up to ``max_retries`` times (exponential
backoff + jitter, honoring server ``Retry-After``), then moves to the next
provider in the chain. Non-retryable errors (4xx other than 429) skip straight
to the next provider. If every provider fails, :class:`AllProvidersFailedError`
carries the full error list.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .. import events as ev
from ..errors import AllProvidersFailedError, ProviderError
from ..events import EventBus
from ..types import Message, ProviderResponse, StreamChunk
from .base import FunctionProvider, LLMProvider


@dataclass
class RetryPolicy:
    """Retry/backoff knobs for :class:`ProviderRouter`: exponential backoff
    from ``base_delay`` capped at ``max_delay``, with proportional jitter;
    a server-provided ``Retry-After`` wins (still capped at ``max_delay``).
    """

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 10.0
    jitter: float = 0.25

    def delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        backoff = min(self.base_delay * (2**attempt), self.max_delay)
        return backoff * (1 + random.uniform(-self.jitter, self.jitter))


class ProviderRouter:
    """Tries each provider in order: retryable failures back off and retry up
    to ``retry_policy.max_retries``, then routing falls through to the next
    provider; when every provider fails, :class:`AllProvidersFailedError`
    carries the accumulated errors.
    """

    def __init__(
        self,
        providers: Sequence[LLMProvider | Callable[..., Any]],
        *,
        retry_policy: RetryPolicy | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        """``providers`` accepts ``LLMProvider`` instances or bare async
        functions — a callable without a ``complete`` attribute is
        auto-wrapped in :class:`FunctionProvider`, same as ``Pipeline.add()``
        auto-wraps a bare function into a ``FunctionStage``."""
        if not providers:
            raise ValueError("ProviderRouter requires at least one provider")
        wrapped = [
            p if hasattr(p, "complete") else FunctionProvider(p)  # type: ignore[arg-type]
            for p in providers
        ]
        self.providers = cast("list[LLMProvider]", wrapped)
        self.retry_policy = retry_policy or RetryPolicy()
        self._bus = event_bus

    async def _emit(self, event: str, **payload: Any) -> None:
        if self._bus:
            await self._bus.emit(event, **payload)

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        errors: list[ProviderError] = []
        for provider in self.providers:
            attempt = 0
            while True:
                await self._emit(
                    ev.PROVIDER_STARTED, provider=provider.name, model=provider.model
                )
                try:
                    response = await provider.complete(messages, **options)
                    await self._emit(
                        ev.PROVIDER_COMPLETED,
                        provider=provider.name,
                        model=response.model,
                        usage=response.usage,
                    )
                    return response
                except ProviderError as exc:
                    errors.append(exc)
                    await self._emit(ev.PROVIDER_FAILED, provider=provider.name, error=str(exc))
                    if not exc.retryable or attempt >= self.retry_policy.max_retries:
                        break
                    await asyncio.sleep(self.retry_policy.delay(attempt, exc.retry_after))
                    attempt += 1
        raise AllProvidersFailedError(
            "; ".join(str(e) for e in errors) or "all providers failed", errors
        )

    async def stream(
        self, messages: list[Message], **options: Any
    ) -> AsyncIterator[StreamChunk]:
        """Stream from the first provider that starts successfully.

        Fallback happens only if a provider fails *before yielding any content*;
        once tokens have been emitted downstream, a mid-stream failure is raised
        as-is (the transport already sent partial output).
        """
        errors: list[ProviderError] = []
        for provider in self.providers:
            attempt = 0
            while True:
                await self._emit(
                    ev.PROVIDER_STARTED, provider=provider.name, model=provider.model
                )
                yielded = False
                try:
                    async for chunk in provider.stream(messages, **options):
                        if chunk.done:
                            await self._emit(
                                ev.PROVIDER_COMPLETED,
                                provider=provider.name,
                                model=chunk.model,
                                usage=chunk.usage,
                            )
                        else:
                            yielded = True
                        yield chunk
                    return
                except ProviderError as exc:
                    errors.append(exc)
                    await self._emit(ev.PROVIDER_FAILED, provider=provider.name, error=str(exc))
                    if yielded:
                        raise
                    if not exc.retryable or attempt >= self.retry_policy.max_retries:
                        break
                    await asyncio.sleep(self.retry_policy.delay(attempt, exc.retry_after))
                    attempt += 1
        raise AllProvidersFailedError(
            "; ".join(str(e) for e in errors) or "all providers failed", errors
        )

    async def close(self) -> None:
        for provider in self.providers:
            await provider.close()

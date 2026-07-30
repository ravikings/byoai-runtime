"""Resilient provider routing: retries with backoff, then fallback across
the provider chain.

The router tries a provider up to ``max_retries`` times (exponential backoff
+ jitter, honoring server ``Retry-After``), then moves to the next provider.
Non-retryable errors (4xx other than 429) skip straight to the next provider.
If every provider fails, :class:`AllProvidersFailedError` carries the full
error list. ``selection`` picks the order providers are tried each call —
fallback always still visits every provider *returned by* ``selection`` in
that order, so reordering alone never skips one. A callable that itself
returns a subset (e.g. dropping providers it considers unhealthy) does
exclude them, by its own choice, not the router's.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from .. import events as ev
from .._config_resolve import resolve_preset
from ..errors import AllProvidersFailedError, ProviderError
from ..events import EventBus
from ..types import Message, ProviderResponse, StreamChunk
from .base import FunctionProvider, LLMProvider

SelectionName = Literal["ordered", "round_robin"]
# (providers) -> providers to try, in order, for this call. Fallback walks
# whatever this returns in full on failure — a provider left out of the
# return value is excluded by the callable's own choice, not tried as a
# fallback of last resort.
SelectionFn = Callable[[Sequence["LLMProvider"]], Sequence["LLMProvider"]]


def _ordered_selection(providers: Sequence["LLMProvider"]) -> Sequence["LLMProvider"]:
    return providers


class _RoundRobinSelection:
    """Rotates the starting provider each call; call N+1 starts where call N
    left off. Stateful per :class:`ProviderRouter` instance — safe under
    concurrent calls only because reordering never awaits."""

    def __init__(self) -> None:
        self._next = 0

    def __call__(self, providers: Sequence["LLMProvider"]) -> Sequence["LLMProvider"]:
        start = self._next % len(providers)
        self._next += 1
        return [*providers[start:], *providers[:start]]


# name -> zero-arg factory producing a fresh selector — "round_robin" needs
# its own per-router _RoundRobinSelection instance, not one shared globally,
# so every preset is a factory rather than a ready-to-use SelectionFn.
_SELECTIONS: dict[str, Callable[[], SelectionFn]] = {
    "ordered": lambda: _ordered_selection,
    "round_robin": _RoundRobinSelection,
}


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
    """Tries providers in the order ``selection`` picks for each call:
    retryable failures back off and retry up to ``retry_policy.max_retries``,
    then routing falls through to the next provider in that order; when every
    provider fails, :class:`AllProvidersFailedError` carries the accumulated
    errors.

    ``selection`` — a preset name or a bare callable:

    * ``"ordered"`` (default) — always try ``providers`` in the order given;
      identical to the router's original fixed-primary/fallback behavior.
    * ``"round_robin"`` — rotates which provider goes first each call, so
      load spreads across providers instead of always preferring the first;
      a failure still falls through the rest in ring order, so nothing is
      skipped, only reprioritized.
    * a bare callable ``(providers) -> providers``, returning the providers
      to try, in order, for this call — e.g. weighted selection. A callable
      that also filters (e.g. dropping providers it considers unhealthy)
      excludes them entirely for that call, by its own choice; it is not a
      pure reprioritization like the two presets above.
    """

    def __init__(
        self,
        providers: Sequence[LLMProvider | Callable[..., Any]],
        *,
        retry_policy: RetryPolicy | None = None,
        selection: SelectionName | SelectionFn = "ordered",
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
        self.selection = selection
        if callable(selection):
            self._select: SelectionFn = selection
        else:
            # _SELECTIONS stores zero-arg factories (not ready-to-use
            # selectors) so "round_robin" gets its own fresh, per-router
            # _RoundRobinSelection instance rather than one shared globally.
            factory = resolve_preset(
                selection,
                _SELECTIONS,
                kind="selection",
                callable_signature="(providers) -> providers",
            )
            self._select = factory()
        self._bus = event_bus

    async def _emit(self, event: str, **payload: Any) -> None:
        if self._bus:
            await self._bus.emit(event, **payload)

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        errors: list[ProviderError] = []
        for provider in self._select(self.providers):
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
        for provider in self._select(self.providers):
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

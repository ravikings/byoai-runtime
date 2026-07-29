"""Onion-style async middleware chain.

Middleware wraps the whole pipeline execution. Each middleware receives the
context and a ``call_next`` continuation; it may mutate the context, skip
``call_next`` entirely to short-circuit (e.g. cache hit, auth rejection), or run
code after the pipeline completes (e.g. tracing, timing).

    class TimingMiddleware(Middleware):
        async def __call__(self, ctx, call_next):
            start = time.monotonic()
            await call_next(ctx)
            ctx.metadata["duration_ms"] = (time.monotonic() - start) * 1000
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .context import RequestContext

CallNext = Callable[[RequestContext], Awaitable[None]]


class Middleware:
    """Base class. Subclass and override ``__call__``; plain async callables with
    the same ``(ctx, call_next)`` signature are also accepted by the chain."""

    async def __call__(self, ctx: RequestContext, call_next: CallNext) -> None:
        await call_next(ctx)


MiddlewareLike = Callable[[RequestContext, CallNext], Awaitable[None]]


class MiddlewareChain:
    def __init__(self) -> None:
        self._middlewares: list[MiddlewareLike] = []

    def add(self, middleware: MiddlewareLike) -> None:
        self._middlewares.append(middleware)

    def __len__(self) -> int:
        return len(self._middlewares)

    async def execute(self, ctx: RequestContext, terminal: CallNext) -> None:
        """Run the chain, innermost call being ``terminal`` (the pipeline)."""

        call: CallNext = terminal
        for middleware in reversed(self._middlewares):
            call = _wrap(middleware, call)
        await call(ctx)


def _wrap(middleware: MiddlewareLike, call_next: CallNext) -> CallNext:
    async def bound(ctx: RequestContext) -> None:
        await middleware(ctx, call_next)

    return bound

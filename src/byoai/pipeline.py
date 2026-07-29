"""Composable execution pipeline.

A pipeline is an ordered list of stages. Each stage receives the
:class:`RequestContext`, does one unit of work (resolve context, look up cache,
retrieve vectors, build a prompt, call a provider, ...) and records its output
on the context. A stage that calls ``ctx.short_circuit()`` stops the remaining
stages from running.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from . import events as ev
from .context import RequestContext
from .errors import ByoAIError, PipelineError
from .events import EventBus


@runtime_checkable
class PipelineStage(Protocol):
    async def execute(self, ctx: RequestContext) -> None: ...


StageLike = "PipelineStage | Callable[[RequestContext], Awaitable[None]]"


class FunctionStage:
    """Adapts a bare async function into a stage."""

    def __init__(self, fn: Callable[[RequestContext], Awaitable[None]], name: str | None = None):
        self._fn = fn
        self.name = name or getattr(fn, "__name__", "function_stage")

    async def execute(self, ctx: RequestContext) -> None:
        await self._fn(ctx)


class Pipeline:
    def __init__(self, name: str = "default") -> None:
        self.name = name
        self._stages: list[PipelineStage] = []

    @property
    def stages(self) -> list[PipelineStage]:
        return list(self._stages)

    def add(self, stage: PipelineStage | Callable[[RequestContext], Awaitable[None]]) -> Pipeline:
        """Append a stage. Accepts stage objects or bare async functions. Chainable."""
        if not hasattr(stage, "execute"):
            stage = FunctionStage(stage)  # type: ignore[arg-type]
        self._stages.append(stage)  # type: ignore[arg-type]
        return self

    def remove(self, stage_type: type) -> Pipeline:
        self._stages = [s for s in self._stages if not isinstance(s, stage_type)]
        return self

    def replace(self, stage_type: type, replacement: PipelineStage) -> Pipeline:
        self._stages = [replacement if isinstance(s, stage_type) else s for s in self._stages]
        return self

    async def execute(self, ctx: RequestContext, bus: EventBus | None = None) -> None:
        for stage in self._stages:
            if ctx.short_circuited:
                break
            stage_name = getattr(stage, "name", type(stage).__name__)
            if bus:
                await bus.emit(ev.STAGE_STARTED, ctx=ctx, stage=stage_name)
            try:
                await stage.execute(ctx)
            except ByoAIError:
                # Typed runtime errors (AllProvidersFailed, ConfigurationError, ...)
                # are part of the public contract — propagate them unwrapped.
                raise
            except Exception as exc:
                raise PipelineError(
                    f"stage {stage_name!r} failed: {exc}", stage=stage_name
                ) from exc
            if bus:
                await bus.emit(ev.STAGE_COMPLETED, ctx=ctx, stage=stage_name)

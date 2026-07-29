"""Async lifecycle event bus.

Every execution emits lifecycle events (``request.received``, ``cache.hit``,
``provider.completed``, ...). Applications subscribe with ``runtime.on()``.

Handlers may be sync or async. Handler failures are isolated: a raising
subscriber never breaks the execution that emitted the event; failures are
reported through the ``error_handler`` hook (default: ignored).
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
from collections import defaultdict
from collections.abc import Callable
from typing import Any

EventHandler = Callable[..., Any]

# Canonical lifecycle event names (applications may emit their own too).
REQUEST_RECEIVED = "request.received"
CONTEXT_RESOLVED = "context.resolved"
CACHE_HIT = "cache.hit"
CACHE_MISS = "cache.miss"
VECTOR_RETRIEVED = "vector.retrieved"
PROVIDER_STARTED = "provider.started"
PROVIDER_COMPLETED = "provider.completed"
PROVIDER_FAILED = "provider.failed"
STAGE_STARTED = "stage.started"
STAGE_COMPLETED = "stage.completed"
RESPONSE_STREAMED = "response.streamed"
REQUEST_COMPLETED = "request.completed"
REQUEST_FAILED = "request.failed"


class EventBus:
    def __init__(self, error_handler: Callable[[str, Exception], None] | None = None) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._wildcard_handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._error_handler = error_handler
        self._pending_tasks: set[asyncio.Task] = set()

    def on(self, event: str, handler: EventHandler) -> Callable[[], None]:
        """Subscribe. ``event`` may contain ``*`` wildcards (e.g. ``provider.*``).

        Returns an unsubscribe function.
        """
        registry = self._wildcard_handlers if "*" in event else self._handlers
        registry[event].append(handler)

        def unsubscribe() -> None:
            try:
                registry[event].remove(handler)
            except ValueError:
                pass

        return unsubscribe

    async def emit(self, event: str, **payload: Any) -> None:
        handlers = list(self._handlers.get(event, ()))
        for pattern, pattern_handlers in self._wildcard_handlers.items():
            if fnmatch.fnmatchcase(event, pattern):
                handlers.extend(pattern_handlers)
        for handler in handlers:
            try:
                result = handler(event, payload)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - subscriber isolation is the contract
                if self._error_handler is not None:
                    self._error_handler(event, exc)

    def emit_nowait(self, event: str, **payload: Any) -> None:
        """Fire-and-forget emit from sync code paths (requires a running loop)."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(self.emit(event, **payload))
        # The loop holds only a weak reference to tasks; keep a strong one
        # until completion or the emit can be garbage-collected mid-flight.
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

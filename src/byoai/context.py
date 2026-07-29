"""Execution-scoped request context.

One :class:`RequestContext` is created per ``runtime.execute()`` call and flows
through every middleware and pipeline stage. Stages communicate exclusively
through it — there is no other shared mutable state in an execution.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .types import Message, Usage


@dataclass
class RequestContext:
    """Carries one request through the runtime.

    ``input`` is the caller's raw payload, untouched. Stages append their
    products to the typed slots below (``messages``, ``documents``, ``response``)
    or to the free-form ``state`` dict for anything stage-specific.
    """

    input: Any
    pipeline_name: str | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str | None = None
    user_id: str | None = None

    # Populated by stages as execution progresses.
    messages: list[Message] = field(default_factory=list)
    documents: list[Any] = field(default_factory=list)
    response: str | None = None
    model: str | None = None
    provider: str | None = None

    # Accounting / control.
    usage: Usage = field(default_factory=Usage)
    cached: bool = False
    short_circuited: bool = False
    started_at: float = field(default_factory=time.monotonic)

    # Free-form scratch space for middleware/stages; namespaced keys encouraged
    # (e.g. "myapp.tenant", "byoai.cache_key").
    state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000.0

    def short_circuit(self, response: str, *, cached: bool = False) -> None:
        """Set a final response and stop remaining pipeline stages from running.

        Used by cache middleware/stages on a hit, or by guardrail middleware
        rejecting a request.
        """
        self.response = response
        self.cached = cached
        self.short_circuited = True

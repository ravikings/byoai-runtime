"""Normalized data types shared across the runtime.

These are deliberately plain dataclasses (not pydantic models) so the core has no
hard dependency beyond the standard library; transport adapters may convert them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover
    from .context import RequestContext

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class Message:
    role: Role
    content: str
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class Usage:
    """Token/cost accounting for one or more provider calls. Additive."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd


@dataclass
class ProviderResponse:
    """Normalized non-streaming completion from any provider adapter."""

    content: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    raw: Any = None


@dataclass
class StreamChunk:
    """One streamed increment. ``delta`` is the new text; ``done`` marks the end.

    ``cached``/``request_id`` are only ever set on the final ``done`` chunk
    Runtime.stream() yields (mirroring ExecutionResult), never on individual
    provider-level deltas."""

    delta: str = ""
    done: bool = False
    model: str | None = None
    provider: str | None = None
    usage: Usage | None = None
    raw: Any = None
    cached: bool = False
    request_id: str | None = None


@dataclass
class ExecutionResult:
    """Final result of ``runtime.execute()``."""

    content: str
    context: RequestContext
    usage: Usage = field(default_factory=Usage)
    cached: bool = False
    model: str | None = None
    provider: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Document:
    """A retrieved vector-store document, normalized across providers."""

    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float | None = None
    embedding: list[float] | None = None

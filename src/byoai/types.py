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
    """``content`` is plain text, a list of provider content blocks (e.g.
    Anthropic ``tool_use``/``tool_result``/``image`` blocks — only the
    Anthropic adapters, ``providers/anthropic.py`` and
    ``providers/anthropic_cloud.py``, are safe to use those with; Gemini/
    OpenAI-compat expect plain strings and will error or mishandle a list),
    or ``None`` on an OpenAI-shaped assistant turn whose only content is a
    tool call (``tool_calls`` set, mirroring OpenAI's own wire format).

    ``tool_call_id``/``tool_calls`` are the OpenAI-compatible-family
    counterpart to Anthropic's content-block tool use: to send a tool result
    back, append the assistant's own tool-call message (``role="assistant"``,
    ``content=None``, ``tool_calls=[{"id": ..., "type": "function",
    "function": {"name": ..., "arguments": "..."}}]`` — the shape
    ``ExecutionResult.raw``/a streamed turn's assembled ``tool_calls`` are
    already in) followed by one ``role="tool"`` message per call
    (``tool_call_id=<call id>``, ``content=<the tool's output as a string>``).
    Anthropic instead round-trips through list-valued ``content`` blocks —
    see ``docs/guides/providers.md`` for both worked examples. Unused by
    Anthropic/Bedrock/Vertex/Gemini.
    """

    role: Role
    content: str | list[dict[str, Any]] | None
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        return d


@dataclass
class Usage:
    """Token/cost accounting for one or more provider calls. Additive."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    # Anthropic prompt-caching accounting — zero for providers/requests that
    # don't use it. Not folded into total_tokens (see property below).
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        # Deliberately input_tokens + output_tokens only, unchanged by the
        # cache_* fields above — Anthropic's own input_tokens already
        # excludes cached/created prefix tokens, so folding them in here
        # would silently change existing callers' cost math.
        return self.input_tokens + self.output_tokens

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens


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
class ToolCallDelta:
    """One incremental fragment of a streamed tool-use block (Anthropic's
    ``content_block_start``/``content_block_delta`` events for a ``tool_use``
    content block; the OpenAI-shaped equivalent is a ``tool_calls[].function``
    delta).

    ``id``/``name`` are set once, on the chunk marking the block's start
    (``partial_json`` is empty there); every following chunk for the same
    ``index`` carries the next raw JSON-fragment in ``partial_json`` and
    leaves ``id``/``name`` unset. Concatenate ``partial_json`` across chunks
    sharing the same ``index`` and ``json.loads()`` once the block closes —
    byoai deliberately does not re-parse the growing fragment into a live
    snapshot on every chunk.
    """

    index: int
    id: str | None = None
    name: str | None = None
    partial_json: str = ""


@dataclass
class StreamChunk:
    """One streamed increment. ``delta`` is the new text; ``done`` marks the end.

    ``tool_call`` is set instead of ``delta`` when the increment is a
    streamed tool-use argument fragment rather than text — the two are
    mutually exclusive per chunk.

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
    finish_reason: str | None = None
    tool_call: ToolCallDelta | None = None


@dataclass
class ExecutionResult:
    """Final result of ``runtime.execute()``.

    Porting from the raw ``anthropic``/``openai`` SDKs: ``content`` here is
    always a flattened ``str``, unlike ``anthropic.Message.content`` (a list
    of typed content blocks) — a pure ``tool_use`` turn comes back as ``""``,
    not a list to iterate. Block-level detail (``tool_use``, response id,
    prompt-cache token counts) lives on ``raw`` instead; see the
    ``docs/guides/providers.md#anthropic-tool-use-and-content-blocks`` guide
    for the per-adapter shape of ``raw`` and a worked tool-use example.
    """

    content: str
    context: RequestContext
    usage: Usage = field(default_factory=Usage)
    cached: bool = False
    model: str | None = None
    provider: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    # Python-API-only escape hatch: the provider adapter's raw response
    # (a dict for the httpx-based adapters, an SDK object for Bedrock/Vertex).
    # Carries anything a normalized field doesn't — e.g. Anthropic tool_use
    # blocks, the provider's own response id. Never JSON-serialize this
    # directly (the SDK-based adapters' raw isn't JSON-safe); transport.py
    # deliberately excludes it from result_to_dict()/chunk_to_dict().
    raw: Any = None


@dataclass
class Document:
    """A retrieved vector-store document, normalized across providers."""

    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float | None = None
    embedding: list[float] | None = None

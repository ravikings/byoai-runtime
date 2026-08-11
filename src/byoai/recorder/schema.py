"""Event schema for the agent recorder.

One immutable record type (:class:`AgentEvent`) plus the helpers that make it
hashable in a way a third party can reproduce: canonical dict form, a
deterministic digest, and the capture clocks from spec §5.5.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .canonical import canonicalize, sha256_hex

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "AgentEvent",
    "EventKind",
    "canonicalize",
    "event_digest",
    "new_event_id",
    "now_monotonic_ns",
    "now_ts_device",
    "sha256_hex",
]

EVENT_SCHEMA_VERSION = "1"


class EventKind(str, Enum):
    """Kinds of event the recorder can seal."""

    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    MESSAGE = "message"
    API_ERROR = "api_error"
    RECORD_FAILURE = "record_failure"
    SESSION_START = "session_start"
    STREAM_ABORTED = "stream_aborted"
    PARSE_FAILURE = "parse_failure"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A single sealed observation about an agent's behaviour.

    ``seq`` is assigned by the ledger at write time; everything else is filled
    in at capture. The instance is frozen because a digest over a mutable
    record is not evidence.
    """

    schema_version: str
    event_id: str
    device_id: str
    session_id: str
    seq: int
    kind: str
    ts_device: str
    ts_monotonic_ns: int
    tool_use_id: str | None
    tool_name: str | None
    payload: dict[str, Any]
    payload_hash: str
    model: str | None
    provider: str

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON dict form. Key order is irrelevant to the digest."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentEvent:
        """Inverse of :meth:`to_dict`. Rejects missing or unknown fields."""
        names = {f.name for f in fields(cls)}
        missing = names - data.keys()
        if missing:
            raise ValueError(f"missing event fields: {sorted(missing)}")
        unknown = data.keys() - names
        if unknown:
            raise ValueError(f"unknown event fields: {sorted(unknown)}")
        return cls(**{name: data[name] for name in names})


def event_digest(event: AgentEvent) -> str:
    """``"sha256:<hex>"`` over the canonical JSON of the whole event.

    Deterministic: canonicalization sorts keys, so the digest does not depend
    on insertion order anywhere in the event or its payload.
    """
    return sha256_hex(canonicalize(event.to_dict()))


def new_event_id() -> str:
    """Fresh ``evt_``-prefixed identifier."""
    return "evt_" + uuid.uuid4().hex


def now_ts_device() -> str:
    """RFC3339 UTC timestamp with microsecond precision, e.g.
    ``2026-08-05T14:22:31.442123Z``. Untrusted host clock — ordering comes
    from ``seq``, never from this value."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}Z"


def now_monotonic_ns() -> int:
    """Monotonic capture counter, immune to wall-clock adjustment."""
    return time.monotonic_ns()

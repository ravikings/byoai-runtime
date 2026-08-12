"""Device-side payload redaction for the recorder's three payload modes.

Spec §7. Client-side only. This module decides what payload *bytes* actually
ship in an event's ``payload`` field once ``payload_hash`` has already been
computed over the raw payload (that hash computation lives elsewhere and is
never touched by this module — see ``recorder.integration``).

Three modes:

- ``full``: payload ships unchanged.
- ``hash-only``: no payload bytes ship at all; only the event's existing
  ``payload_hash`` travels.
- ``redacted`` (default): a deep copy of the payload with secrets/PII masked
  in place, and every other value that isn't a short "structural" token
  replaced by a salted digest so equal values are linkable within one
  session without being dictionary-reversible.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any

__all__ = ["PayloadMode", "apply_payload_mode"]


class PayloadMode(str, Enum):
    HASH_ONLY = "hash-only"
    REDACTED = "redacted"
    FULL = "full"


# ------------------------------------------------------------- detection

_HIGH_ENTROPY_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")
_SK_PREFIX_RE = re.compile(r"^sk-[A-Za-z0-9_-]+$")
_BEARER_RE = re.compile(r"^Bearer\s+\S+$", re.IGNORECASE)
_AWS_KEY_RE = re.compile(r"^AKIA[0-9A-Z]{16}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_CREDIT_CARD_RUN_RE = re.compile(r"\b\d{13,19}\b")


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _detect_credit_card(value: str) -> bool:
    for match in _CREDIT_CARD_RUN_RE.finditer(value):
        if _luhn_valid(match.group(0)):
            return True
    return False


def _detect_kind(value: str) -> str | None:
    """Return the redaction ``kind`` label if ``value`` matches a known secret/PII pattern."""
    if _SK_PREFIX_RE.match(value):
        return "api_key"
    if _BEARER_RE.match(value):
        return "api_key"
    if _AWS_KEY_RE.match(value):
        return "aws_key"
    if _EMAIL_RE.match(value):
        return "email"
    if _SSN_RE.match(value):
        return "ssn"
    if _detect_credit_card(value):
        return "credit_card"
    if _HIGH_ENTROPY_RE.match(value):
        # High-entropy strings are treated as opaque secrets regardless of
        # key name — over-redacting is the safe failure mode.
        return "api_key"
    return None


def _salted_digest(session_salt: str, value: Any) -> str:
    digest = hashlib.sha256(f"{session_salt}:{value!s}".encode()).hexdigest()
    return f"[REDACTED:hash:{digest[:16]}]"


def _redact_string(value: str, *, session_salt: str) -> str:
    kind = _detect_kind(value)
    if kind is not None:
        return f"[REDACTED:{kind}]"
    # Nothing here is an obviously safe structural token — hash it too
    # rather than let raw bytes off the device.
    return _salted_digest(session_salt, value)


def _redact_value(value: Any, *, session_salt: str) -> Any:
    if isinstance(value, dict):
        return {k: _redact_value(v, session_salt=session_salt) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, session_salt=session_salt) for v in value]
    if isinstance(value, str):
        return _redact_string(value, session_salt=session_salt)
    if isinstance(value, bool):
        return _salted_digest(session_salt, value)
    if isinstance(value, (int, float)):
        return _salted_digest(session_salt, value)
    # None and any other structural/opaque value pass through unchanged.
    return value


def apply_payload_mode(
    payload: dict[str, Any],
    mode: PayloadMode,
    *,
    session_salt: str,
) -> dict[str, Any]:
    """Return what actually ships in the event's ``payload`` field for ``mode``.

    - FULL: returns ``payload`` unchanged.
    - HASH_ONLY: returns ``{}`` (no payload bytes ship at all — only the
      event's existing top-level ``payload_hash``, which callers must still
      compute over the RAW payload before calling this function).
    - REDACTED: returns a deep copy of ``payload`` with detected secrets/PII
      masked in place, and low-entropy/unclassified values replaced with a
      salted digest.
    """
    if mode is PayloadMode.FULL:
        return payload
    if mode is PayloadMode.HASH_ONLY:
        return {}
    if mode is PayloadMode.REDACTED:
        return {k: _redact_value(v, session_salt=session_salt) for k, v in payload.items()}
    raise ValueError(f"unknown payload mode: {mode!r}")

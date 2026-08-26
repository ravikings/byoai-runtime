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

Free text is a second, deliberately different case (``redact_free_text``,
below). A decision's synthesized prose — e.g. a trace's ``output`` — is meant
to stay human-readable for an auditor, so it is never hashed wholesale the
way a structured field is. Instead it is scanned for the same known
secret/PII *kinds* as above wherever they appear as a substring, and only
those spans are masked. This still narrows what leaves the device under
``redacted``/``hash-only``, but it is not a general PII scrubber: anything
that isn't email/SSN/credit-card/API-key shaped (a name, a street address, a
free-text account number) passes through untouched, because there is no
pattern to match it against. Callers that hand an LLM's own words to this
module should not treat a clean scan as a guarantee the text is free of PII.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any

__all__ = ["PayloadMode", "apply_payload_mode", "redact_free_text"]


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

# Unanchored counterparts of the patterns above, for scanning a PII kind out
# of the middle of a sentence rather than matching a whole field value. Order
# matters in `_SCAN_PATTERNS` below: more specific patterns run first so a
# generic high-entropy token doesn't eat a match a later, narrower pattern
# would have labeled correctly.
_SK_SCAN_RE = re.compile(r"sk-[A-Za-z0-9_-]{10,}")
_BEARER_SCAN_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_AWS_KEY_SCAN_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_EMAIL_SCAN_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN_SCAN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_HIGH_ENTROPY_SCAN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")

_SCAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key", _SK_SCAN_RE),
    ("api_key", _BEARER_SCAN_RE),
    ("aws_key", _AWS_KEY_SCAN_RE),
    ("email", _EMAIL_SCAN_RE),
    ("ssn", _SSN_SCAN_RE),
)


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


def _credit_card_spans(text: str) -> list[tuple[int, int, str]]:
    return [
        (m.start(), m.end(), "credit_card")
        for m in _CREDIT_CARD_RUN_RE.finditer(text)
        if _luhn_valid(m.group(0))
    ]


def _scan_spans(text: str) -> list[tuple[int, int, str]]:
    spans = _credit_card_spans(text)
    for kind, pattern in _SCAN_PATTERNS:
        spans.extend((m.start(), m.end(), kind) for m in pattern.finditer(text))
    # High-entropy is the least specific pattern (spec: over-redacting is the
    # safe failure mode), so it goes last and only fills gaps the patterns
    # above didn't already claim.
    spans.extend((m.start(), m.end(), "api_key") for m in _HIGH_ENTROPY_SCAN_RE.finditer(text))

    # Resolve overlaps: earliest start wins, ties broken by the longer match
    # (a Bearer-token match should win over a high-entropy match nested
    # inside it). Sorted so a single left-to-right sweep can drop anything
    # that overlaps what's already been kept.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept: list[tuple[int, int, str]] = []
    cursor = 0
    for start, end, kind in spans:
        if start < cursor:
            continue
        kept.append((start, end, kind))
        cursor = end
    return kept


def redact_free_text(text: str) -> str:
    """Mask known secret/PII substrings inside a block of free text.

    Unlike :func:`apply_payload_mode`'s ``redacted`` handling of structured
    fields, this never hashes text it doesn't recognize — the whole point of
    a field like a decision's ``output`` is that a human can still read it
    afterward. Only spans matching a known kind (email, SSN, credit card,
    API key, AWS key, or a bare high-entropy token) are replaced with
    ``[REDACTED:<kind>]``; everything else, including names and other PII
    with no fixed shape, ships unchanged. See the module docstring for why
    that's a real limit, not an oversight.
    """
    spans = _scan_spans(text)
    if not spans:
        return text
    out: list[str] = []
    cursor = 0
    for start, end, kind in spans:
        out.append(text[cursor:start])
        out.append(f"[REDACTED:{kind}]")
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


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

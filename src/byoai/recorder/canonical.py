"""RFC 8785 (JCS) JSON canonicalization plus the hash helper built on it.

Hashes in the ledger must be reproducible by a third party, so serialization
cannot depend on Python's ``json`` defaults (key order, unicode escaping and
float formatting are all runtime-specific). This module implements JCS:

* UTF-8 output, no insignificant whitespace;
* object keys sorted by UTF-16 code unit, not by Python code point order
  (they differ for astral-plane characters);
* ECMAScript ``Number::toString`` for numbers (``1.0`` -> ``1``,
  ``1e21`` -> ``1e+21``);
* the RFC 8785 / JSON string escaping rules;
* ``NaN`` and the infinities are rejected outright.
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from typing import Any

__all__ = [
    "CanonicalizationError",
    "canonicalize",
    "canonical_dumps",
    "sha256_hex",
    "serialize_number",
]


class CanonicalizationError(ValueError):
    """Raised for values that RFC 8785 cannot canonicalize."""


# --------------------------------------------------------------------------
# strings
# --------------------------------------------------------------------------

_SHORT_ESCAPES = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def _serialize_string(value: str) -> str:
    out: list[str] = ['"']
    for ch in value:
        cp = ord(ch)
        short = _SHORT_ESCAPES.get(cp)
        if short is not None:
            out.append(short)
        elif cp < 0x20:
            out.append(f"\\u{cp:04x}")
        elif 0xD800 <= cp <= 0xDFFF:
            raise CanonicalizationError(
                f"lone surrogate U+{cp:04X} is not valid JSON text"
            )
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_sort_key(key: str) -> bytes:
    """Sort key giving lexicographic order over UTF-16 code units.

    Comparing big-endian UTF-16 byte sequences is equivalent to comparing the
    code unit sequences, which is what RFC 8785 mandates. Plain Python string
    comparison is code-point order and disagrees for astral-plane characters
    (a surrogate pair starts at U+D800, below U+E000 and friends).
    """
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:  # lone surrogate in a key
        raise CanonicalizationError(f"invalid object key {key!r}") from exc


# --------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------


def serialize_number(value: float | int) -> str:
    """ECMAScript ``Number::toString`` (radix 10) for a JSON number."""
    if isinstance(value, bool):  # pragma: no cover - guarded by caller
        raise CanonicalizationError("bool is not a number")
    if isinstance(value, int):
        # Exact integers are emitted exactly. RFC 8785 assumes IEEE-754
        # doubles; Python ints beyond 2**53 keep full precision here, which is
        # a deliberate superset (see module docs in the recorder contract).
        return str(value)

    value = float(value)
    if math.isnan(value) or math.isinf(value):
        raise CanonicalizationError("NaN and Infinity are not valid JSON numbers")
    if value == 0.0:
        return "0"  # also collapses -0.0, per ECMAScript
    if value < 0:
        return "-" + serialize_number(-value)

    # repr() is the shortest round-tripping decimal form, which is exactly the
    # digit string ECMAScript's algorithm selects.
    _, digits, exponent = Decimal(repr(value)).as_tuple()
    exponent = int(exponent)
    digit_list = list(digits)
    while len(digit_list) > 1 and digit_list[-1] == 0:
        digit_list.pop()
        exponent += 1
    s = "".join(str(d) for d in digit_list)
    k = len(s)
    n = exponent + k  # value == 0.s * 10**n

    if k <= n <= 21:
        return s + "0" * (n - k)
    if 0 < n <= 21:
        return s[:n] + "." + s[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + s
    # exponential notation
    e = n - 1
    sign_char = "+" if e >= 0 else "-"
    mantissa = s if k == 1 else s[0] + "." + s[1:]
    return f"{mantissa}e{sign_char}{abs(e)}"


# --------------------------------------------------------------------------
# values
# --------------------------------------------------------------------------


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_serialize_string(value))
    elif isinstance(value, (int, float)):
        out.append(serialize_number(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        items = sorted(value.items(), key=lambda kv: _utf16_sort_key(_check_key(kv[0])))
        for i, (key, item) in enumerate(items):
            if i:
                out.append(",")
            out.append(_serialize_string(key))
            out.append(":")
            _serialize(item, out)
        out.append("}")
    else:
        raise CanonicalizationError(
            f"cannot canonicalize object of type {type(value).__name__}"
        )


def _check_key(key: Any) -> str:
    if not isinstance(key, str):
        raise CanonicalizationError(
            f"object keys must be strings, got {type(key).__name__}"
        )
    return key


def canonical_dumps(obj: Any) -> str:
    """Canonical JSON as ``str``. See :func:`canonicalize` for bytes."""
    out: list[str] = []
    _serialize(obj, out)
    return "".join(out)


def canonicalize(obj: Any) -> bytes:
    """Serialize ``obj`` to RFC 8785 canonical JSON, encoded as UTF-8."""
    return canonical_dumps(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """SHA-256 of ``data`` in the ledger's ``"sha256:<hex>"`` form."""
    return "sha256:" + hashlib.sha256(data).hexdigest()

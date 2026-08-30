"""cap-digest-v1: the canonicalisation Coriqo's capability-snapshot endpoint
recomputes and compares against, so a byte-for-byte port here is what makes
``POST .../capability-snapshot`` succeed instead of 422ing on every call.

This is a **port**, not a copy, but as of the parity fix below it is a port
of Coriqo's *actual* canonicalisation algorithm
(``api/utils/capability_digest.py``), not of RFC 8785 (JCS). Both this
module and Coriqo's are Python, so re-implementing Coriqo's own
``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` +
integral-float-to-int pre-pass here — rather than routing through this
repo's shared JCS module — gives byte-identical output by construction
(same language, same stdlib float/dict-sort behaviour), not just for the
cases anyone thought to test.

.. warning::
    **Parity bug found and fixed 2026-08-30 (round 2)**, by an audit that
    read both implementations side by side rather than trusting the golden
    vectors already in each repo's own test suite (which only prove
    self-consistency, not cross-repo agreement). This module previously
    delegated to :mod:`byoai.recorder.canonical`, this repo's RFC 8785 (JCS)
    canonicalizer — appropriate for its actual job (this repo's ledger
    hashing) but NOT what Coriqo's ``cap-digest-v1`` spec actually is. Two
    concrete divergences confirmed:

    1. **Non-integral float formatting.** Coriqo emits Python's
       ``repr()``/``json.dumps`` shortest-round-trip form (e.g. ``1e-07``,
       two-digit zero-padded exponent). JCS's ``serialize_number`` instead
       emits ECMAScript ``Number::toString`` form (e.g. ``1e-7``, no
       padding, and a different fixed/exponential threshold — JCS switches
       to exponential outside ``-6 < n <= 21``, Python's repr switches
       outside roughly ``-4 <= decpt <= 16``). A tool ``input_schema`` with
       a small-magnitude float (an epsilon/threshold parameter, a
       genuinely plausible real value) would 422 every attestation.
    2. **Dict key ordering for astral-plane characters.** JCS sorts by
       UTF-16 code unit; Coriqo's ``json.dumps(sort_keys=True)`` sorts by
       Python code point. These *agree* for BMP-only keys but diverge when
       one sibling key is in U+E000–U+FFFF and another is above U+FFFF (an
       astral character's leading surrogate, 0xD800, sorts before 0xE000 in
       UTF-16-code-unit order but above it in code-point order). Rare in
       practice (an ASCII tool/field name never hits this), but real.

    Both are structural consequences of reusing a *different, correctly
    implemented* canonicalisation spec (JCS) for a job that is actually
    "match this other Python program's `json.dumps` output" — no amount of
    JCS correctness closes that gap, because JCS and Coriqo's ad hoc
    ``json.dumps``-based scheme are two different, only-mostly-compatible
    specs. Using Python's own json.dumps here, as this module now does,
    removes the gap entirely rather than chasing individual divergences.

.. warning::
    **Parity bug found and fixed 2026-08-30 (round 1)**, once Coriqo's
    actual ``api/utils/capability_digest.py::compute_capability_digest``
    became readable from a sibling checkout. This module used to hash
    ``system_prompt is None`` the same as an empty string. Coriqo embeds a
    literal JSON ``null`` for ``system_prompt_sha256`` when the prompt is
    ``None`` (and reports ``system_prompt_chars=None``, not ``0``) — never a
    hash of the empty string. Since ``system_prompt_sha256`` is hashed
    *inside* the envelope, that mismatch would have made every digest from a
    promptless agent disagree with the server's recomputation, 422ing on
    every attestation from an agent with no system prompt. This module now
    mirrors Coriqo's convention exactly: ``system_prompt_sha256(None)``
    returns ``None``, and the envelope embeds JSON ``null`` in that slot
    rather than a hash of ``""``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "DIGEST_SPEC",
    "canonical_json_bytes",
    "canonicalize_tool",
    "canonicalize_tools",
    "compute_capability_digest",
    "system_prompt_sha256",
]

#: The literal spec identifier embedded inside the hashed envelope, and sent
#: alongside the digest so Coriqo knows which canonicalisation version to
#: recompute against.
DIGEST_SPEC = "cap-digest-v1"


def _canon(value: Any) -> Any:
    """Port of Coriqo's ``api/utils/capability_digest.py::_canon`` — recursively
    normalise a JSON-compatible value, folding any integral-valued float
    (``1.0``) into its ``int`` form (``1``) so the two serialize identically.
    Dict key sorting itself is left to ``json.dumps(sort_keys=True)`` at the
    serialize step, matching Coriqo exactly (both are Python, both get
    code-point order — never UTF-16-code-unit/JCS order)."""
    if isinstance(value, Mapping):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return int(value)
        return value
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Coriqo's exact canonical-JSON recipe: sorted keys, no insignificant
    whitespace, UTF-8, non-ASCII left literal (not \\uXXXX-escaped), floats
    via Python's own shortest-round-trip ``repr``/``json.dumps`` — never
    JCS/ECMAScript formatting, which disagrees with Python's for
    small-magnitude floats and for dict-key order among astral-plane
    characters (see the module-level parity-bug warning)."""
    canon = _canon(value)
    text = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def canonicalize_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    """One tool, reduced to exactly ``{name, description, input_schema}``.

    ``name`` is required (``tool["name"]``, raising if absent) — matching
    Coriqo's ``canonicalise_tool`` exactly, which never treats a nameless
    tool as valid input to canonicalise. ``description``/``input_schema``
    default to ``None`` when absent so that "missing" and "explicit null"
    always serialize identically.
    """
    return {
        "name": tool["name"],
        "description": tool.get("description"),
        "input_schema": tool.get("input_schema"),
    }


def canonicalize_tools(tools: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every tool canonicalised, then sorted by ``name`` (bytewise/lexicographic).

    Sorting happens on the canonicalised name (coerced to ``str``, ``""`` if
    the tool has none — a tool without a name is malformed input, and this
    keeps the sort defined rather than raising deep inside a digest
    computation), so two lists differing only in source order hash the same.

    Python's default string ordering on ``str`` is already ordinal
    (code-point, i.e. bytewise for the ASCII tool names this is expected to
    see) — the same order ``sorted()`` on plain ``bytes`` would give for pure
    ASCII input.
    """
    canonical = [canonicalize_tool(tool) for tool in tools]
    canonical.sort(key=lambda t: str(t.get("name") or ""))
    return canonical


def system_prompt_sha256(system_prompt: str | None) -> str | None:
    """Hex sha256 of the UTF-8 bytes of ``system_prompt``, or ``None``.

    ``None`` in, ``None`` out — matching Coriqo's
    ``compute_capability_digest`` exactly (see the module-level parity-bug
    warning). A prompt of ``""`` still hashes to a real, non-``None`` digest;
    only the true absence of a prompt propagates as ``None``, both in the
    returned value and in the envelope this feeds (JSON ``null``, not a hash
    of empty content).
    """
    if system_prompt is None:
        return None
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def compute_capability_digest(
    *,
    tools: Iterable[Mapping[str, Any]],
    system_prompt: str | None,
    model_id: str | None,
) -> tuple[str, str | None]:
    """Returns ``(digest_hex, system_prompt_sha256_hex_or_none)``.

    ``digest_hex`` is ``sha256_hex(canonical_json(envelope))`` where
    ``envelope`` is::

        {"spec": "cap-digest-v1", "tools": [...], "system_prompt_sha256": ...,
         "model_id": ...}

    with ``tools`` already reduced/sorted by :func:`canonicalize_tools` and
    ``system_prompt_sha256`` from :func:`system_prompt_sha256` — which is
    ``None`` (JSON ``null`` in the envelope) when ``system_prompt is None``,
    matching Coriqo's own envelope construction exactly. The literal string
    ``"cap-digest-v1"`` is embedded inside the hashed payload, per spec,
    rather than only sent alongside it as ``digest_spec`` — so a request that
    flips ``digest_spec`` without also changing what got hashed cannot
    produce a digest Coriqo would accept.

    ``tools`` and ``system_prompt`` are the only inputs the spec says the
    digest covers; ``runtime_version`` deliberately does not enter the
    envelope (Coriqo's contract only names ``model_id`` inside it).
    """
    prompt_hash = system_prompt_sha256(system_prompt)
    envelope = {
        "spec": DIGEST_SPEC,
        "tools": canonicalize_tools(tools),
        "system_prompt_sha256": prompt_hash,
        "model_id": model_id,
    }
    digest_hex = hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()
    return digest_hex, prompt_hash

"""cap-digest-v1: the canonicalisation Coriqo's capability-snapshot endpoint
recomputes and compares against, so a byte-for-byte port here is what makes
``POST .../capability-snapshot`` succeed instead of 422ing on every call.

This is a **port**, not a copy: Coriqo's own implementation is in a separate
repository this module's author never saw (see the module-level warning
below). It leans on :mod:`byoai.recorder.canonical`, this repo's existing
RFC 8785 (JCS) canonical-JSON implementation, because JCS already gives the
two properties the spec calls out as tricky:

* object keys sorted (JCS: by UTF-16 code unit — a strict superset of
  bytewise ASCII sorting, which is all a tool ``name`` is expected to use);
* ECMAScript ``Number::toString`` number formatting, which is exactly what
  makes ``1.0`` and ``1`` serialize identically (:func:`serialize_number`
  collapses a float with no fractional part to its integer digits).

What this module adds on top of ``canonical.py`` is spec-specific shaping:
picking exactly the three tool fields, sorting tools by name, and the
envelope/system-prompt-hash conventions below.

.. warning::
    **Cross-repo parity is unverified.** This was built against the wire
    contract in the W-7 spec, without sight of Coriqo's actual
    ``cap-digest-v1`` implementation or its fixture file. The tests in
    ``tests/recorder/test_capability_digest.py`` only prove *internal*
    consistency (order-independence, null/absent equivalence, int/float
    equivalence) — they cannot byte-compare against a real Coriqo digest.
    Treat every attestation as unverified until both sides have been run
    against the same golden vectors in one place.

None-handling for ``system_prompt`` (this repo's choice, not dictated by the
spec, which left it "TBD, check the convention"): hash the empty byte string,
the same as an empty prompt. That makes "no prompt" and "empty-string prompt"
hash identically, which is deliberate — neither carries any content for a
downstream drift check to react to — and keeps the hash defined and stable
(a fixed 64-hex-char output, never ``None``) instead of forcing every
consumer to special-case a missing hash.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from .canonical import canonicalize

__all__ = [
    "DIGEST_SPEC",
    "canonicalize_tool",
    "canonicalize_tools",
    "compute_capability_digest",
    "system_prompt_sha256",
]

#: The literal spec identifier embedded inside the hashed envelope, and sent
#: alongside the digest so Coriqo knows which canonicalisation version to
#: recompute against.
DIGEST_SPEC = "cap-digest-v1"

#: The only three fields cap-digest-v1 hashes per tool. Anything else on a
#: tool definition (e.g. a provider-specific ``cache_control`` block) is
#: dropped before hashing — the spec says "drop any other fields" exactly so
#: that provider-shape noise can't perturb the digest.
_TOOL_FIELDS = ("name", "description", "input_schema")


def canonicalize_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    """One tool, reduced to exactly ``{name, description, input_schema}``.

    Absent ``description``/``input_schema`` becomes an explicit ``None``
    (which :mod:`canonical` serializes as JSON ``null``) rather than being
    left out of the dict — the spec requires omitted and explicit-null to
    produce byte-identical output, and this is what makes that true: both
    cases reach ``canonicalize()`` as the same three-key dict.
    """
    return {field: tool.get(field) for field in _TOOL_FIELDS}


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


def system_prompt_sha256(system_prompt: str | None) -> str:
    """Hex sha256 of the UTF-8 bytes of ``system_prompt``.

    ``None`` hashes the same as ``""`` — see the module docstring's
    None-handling note. This is this repo's own documented convention where
    the spec left it open; it produces a stable, deterministic hash (and an
    implied char count of 0) whether the agent has no system prompt at all or
    an explicitly empty one, rather than requiring a sentinel hash value.
    """
    content = system_prompt or ""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_capability_digest(
    *,
    tools: Iterable[Mapping[str, Any]],
    system_prompt: str | None,
    model_id: str | None,
) -> tuple[str, str]:
    """Returns ``(digest_hex, system_prompt_sha256_hex)``.

    ``digest_hex`` is ``sha256_hex(canonical_json(envelope))`` where
    ``envelope`` is::

        {"spec": "cap-digest-v1", "tools": [...], "system_prompt_sha256": ...,
         "model_id": ...}

    with ``tools`` already reduced/sorted by :func:`canonicalize_tools` and
    ``system_prompt_sha256`` from :func:`system_prompt_sha256`. The literal
    string ``"cap-digest-v1"`` is embedded inside the hashed payload, per
    spec, rather than only sent alongside it as ``digest_spec`` — so a
    request that flips ``digest_spec`` without also changing what got hashed
    cannot produce a digest Coriqo would accept.

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
    digest_hex = hashlib.sha256(canonicalize(envelope)).hexdigest()
    return digest_hex, prompt_hash

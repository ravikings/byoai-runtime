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
    **Cross-repo parity is unverified beyond the fix below.** This was built
    against the wire contract in the W-7 spec, without sight of Coriqo's
    actual ``cap-digest-v1`` implementation or its fixture file. The tests in
    ``tests/recorder/test_capability_digest.py`` only prove *internal*
    consistency (order-independence, null/absent equivalence, int/float
    equivalence) — they cannot byte-compare against a real Coriqo digest.
    Treat every attestation as unverified until both sides have been run
    against the same golden vectors in one place.

.. warning::
    **Parity bug found and fixed 2026-08-30**, once Coriqo's actual
    ``api/utils/capability_digest.py::compute_capability_digest`` became
    readable from a sibling checkout. The first version of this module hashed
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
    digest_hex = hashlib.sha256(canonicalize(envelope)).hexdigest()
    return digest_hex, prompt_hash

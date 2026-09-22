"""CEI chain-head computation, ported from Coriqo's own algorithm.

Coriqo's ``api/chain.py``/``api/domains/agents/attestation_ingest.py``
compute a CEI envelope's ``chain_head`` as:

    H_0 = GENESIS_PREV_HASH  ("0" * 64)
    H_n = sha256_hex(H_{n-1}.encode("utf-8") + canonical_bytes(event_n))

over events already in ascending ``seq`` order (Coriqo's ingest refuses to
sort for you — see ``attestation_ingest.compute_chain_head``'s docstring).
This is a straight port of that function, byte-for-byte, so byoai-runtime
can compute the same ``chain_head`` locally that Coriqo will compute
server-side for the same event list, and detect drift before shipping.

Canonicalization: Coriqo's ``canonical_bytes()`` is Python's ``json.dumps``
with ``sort_keys=True``, no insignificant whitespace, UTF-8. This repo's
``canonical.py`` implements RFC 8785 (JCS) instead, which differs from
Coriqo's scheme in number formatting and non-BMP key ordering in principle
— but AIR-4 already pinned byte-identical output between the two for the
payload-hash case (dict of JSON-safe primitives, ASCII/BMP keys, no floats
needing ECMAScript-specific formatting), which is the same shape of input a
CEI event is. We reuse ``canonicalize()`` here on that same basis rather
than duplicating a second canonicalizer; the parity fixture in
``tests/recorder/test_attestation.py`` is what actually proves it for the
chain-head computation specifically, not just asserted by this comment.

The hash itself must be raw lowercase hex, with NO ``"sha256:"`` prefix —
that prefix is this repo's own local-ledger convention
(``canonical.sha256_hex``), not Coriqo's. Reusing that helper here would
produce a value that no longer matches ``chain_head`` byte-for-byte, so this
module hashes directly with :mod:`hashlib` instead.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .canonical import canonicalize

__all__ = [
    "GENESIS_PREV_HASH",
    "compute_chain_head",
]

# Coriqo's api/chain.py::GENESIS_PREV_HASH — the "previous hash" of the
# first event in a chain. Not to be confused with this repo's own
# GENESIS_PREV_HASH in ledger.py ("sha256:" + "00" * 32), which is a
# different, local-checkpoint-chain convention.
GENESIS_PREV_HASH = "0" * 64


def _sha256_hex_raw(data: bytes) -> str:
    """Raw lowercase hex digest, matching Coriqo's api/chain.py::sha256_hex
    exactly (no ``"sha256:"`` prefix — see module docstring)."""
    return hashlib.sha256(data).hexdigest()


def compute_chain_head(events: list[dict[str, Any]]) -> str:
    """Port of Coriqo's ``attestation_ingest.compute_chain_head()``.

    ``H_0 = GENESIS_PREV_HASH``; ``H_n = sha256_hex(H_{n-1} + canonical_bytes(event_n))``.

    Events must already be in ascending ``seq`` order — this function does
    not sort, mirroring Coriqo's own refusal to sort for the emitter (a
    chain that only verifies after the *server* reorders it is not evidence
    of order, it is evidence of the server's own sort). An empty list
    returns ``GENESIS_PREV_HASH`` unchanged, same as Coriqo's ``EventChain``
    with no events.
    """
    prev = GENESIS_PREV_HASH
    for event in events:
        prev = _sha256_hex_raw(prev.encode("utf-8") + canonicalize(event))
    return prev

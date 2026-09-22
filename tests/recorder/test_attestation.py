"""AIR-7a: compute_chain_head() must byte-match Coriqo's own computation.

The fixture values below were produced by running Coriqo's real
``api/domains/agents/attestation_ingest.compute_chain_head()`` (which uses
``api/chain.py``'s ``canonical_bytes``/``sha256_hex``/``GENESIS_PREV_HASH``)
against the exact event lists below, in the coriqo repo, on 2026-09-22:

    cd coriqo && python3 -c "
    from api.domains.agents.attestation_ingest import compute_chain_head
    events = [...]  # see FIXTURE_EVENTS below
    print(compute_chain_head(events))
    "

This is not a self-consistency check — it pins against Coriqo's real
server-side output, so a future change to either repo's canonicalization
that breaks parity fails this test instead of failing silently in
production as a refused attestation.
"""

from __future__ import annotations

import copy

from byoai.recorder.attestation import GENESIS_PREV_HASH, compute_chain_head

# Exact input given to Coriqo's compute_chain_head() to produce the fixture
# chain_head below. Keep in sync with the docstring's repro command.
FIXTURE_EVENTS = [
    {
        "seq": 0,
        "event_type": "TOOL_CALL",
        "ts": "2026-09-22T10:00:00Z",
        "payload_hash": "abc123",
        "resource": "tool:payments.refund",
        "on_behalf_of": ["agent_A"],
    },
    {
        "seq": 1,
        "event_type": "TOOL_RESULT",
        "ts": "2026-09-22T10:00:01Z",
        "payload_hash": "def456",
    },
    {
        "seq": 2,
        "event_type": "TOOL_CALL",
        "ts": "2026-09-22T10:00:02Z",
        "payload_hash": "ghi789",
        "resource": "tool:accounts.lookup",
        "on_behalf_of": [],
    },
]

# Coriqo's real compute_chain_head(FIXTURE_EVENTS) output, hardcoded.
CORIQO_CHAIN_HEAD = "a1abae04bc7f13efe629aa467583bbeb9fecd46a5a7628a41dc5c016c815f4c2"

# Coriqo's compute_chain_head(FIXTURE_EVENTS with events[1]'s payload_hash
# changed to "TAMPERED"), hardcoded — proves tamper-evidence and pins the
# same parity for a second, distinct input.
CORIQO_TAMPERED_CHAIN_HEAD = "8738d7a427878e6d637bcabe69a5aca3e43ba859561d743d3ea029ffe2888e11"

# Coriqo's compute_chain_head([]) — an empty chain returns GENESIS_PREV_HASH
# unchanged.
CORIQO_EMPTY_CHAIN_HEAD = "0" * 64


def test_genesis_prev_hash_matches_coriqo():
    assert GENESIS_PREV_HASH == "0" * 64


def test_chain_head_byte_matches_coriqo_output():
    """The parity pin: byoai-runtime's compute_chain_head() over the fixture
    events must equal Coriqo's own compute_chain_head() over the same
    events, byte-for-byte."""
    assert compute_chain_head(FIXTURE_EVENTS) == CORIQO_CHAIN_HEAD


def test_empty_events_returns_genesis():
    assert compute_chain_head([]) == CORIQO_EMPTY_CHAIN_HEAD


def test_tamper_evidence_changes_chain_head():
    """A single tampered field downstream of the tamper point changes the
    chain_head, and the new value matches what Coriqo itself computes for
    the same tampered input — not just "some other value"."""
    tampered = copy.deepcopy(FIXTURE_EVENTS)
    tampered[1]["payload_hash"] = "TAMPERED"

    tampered_head = compute_chain_head(tampered)

    assert tampered_head != CORIQO_CHAIN_HEAD
    assert tampered_head == CORIQO_TAMPERED_CHAIN_HEAD


def test_chain_head_does_not_mutate_input():
    original = copy.deepcopy(FIXTURE_EVENTS)
    compute_chain_head(FIXTURE_EVENTS)
    assert FIXTURE_EVENTS == original

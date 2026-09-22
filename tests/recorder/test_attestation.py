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

from byoai.recorder.attestation import GENESIS_PREV_HASH, build_envelope, compute_chain_head
from byoai.recorder.canonical import canonicalize
from byoai.recorder.canonical import sha256_hex as local_sha256_hex
from byoai.recorder.ledger import Ledger
from byoai.recorder.schema import EventKind

from .conftest import make_event

DEVICE = "dev_envelope_test"

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


# --------------------------------------------------------------------------
# AIR-7c: build_envelope()


def _verdict_event(*, resource: str | None, on_behalf_of: list[str] | None):
    payload = {"tool": resource, "verdict": "allowed", "reason": "in_scope"}
    if resource is not None:
        payload["resource"] = resource
    if on_behalf_of is not None:
        payload["on_behalf_of"] = on_behalf_of
    return make_event(DEVICE, kind=EventKind.MANDATE_VERDICT, payload=payload)


def _window():
    return {"started_at": "2026-09-22T10:00:00.000000Z", "ended_at": "2026-09-22T10:00:05.000000Z"}


def _agent():
    return {"agent_id": "agent_123", "agent_version": "1.0.0"}


def _device():
    return {"kind": "proxy", "vendor": "byoai-runtime", "software_version": "0.1.0"}


def test_only_attestable_kinds_are_included(tmp_path):
    led = Ledger(tmp_path / "ledger.db", DEVICE)
    try:
        led.append(make_event(DEVICE, kind=EventKind.TOOL_USE))
        led.append(_verdict_event(resource="tool:payments.refund", on_behalf_of=["agent_A"]))
        led.append(make_event(DEVICE, kind=EventKind.MESSAGE))
        led.append(_verdict_event(resource="tool:accounts.lookup", on_behalf_of=[]))
        entries = led.read_range(1, 4)
    finally:
        led.close()

    envelope = build_envelope(entries, agent=_agent(), device=_device(), window=_window())

    assert [e["event_type"] for e in envelope["events"]] == ["MANDATE_VERDICT", "MANDATE_VERDICT"]
    assert [e["resource"] for e in envelope["events"]] == [
        "tool:payments.refund",
        "tool:accounts.lookup",
    ]


def test_seq_is_renumbered_fresh_per_window_not_copied_from_the_ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.db", DEVICE)
    try:
        led.append(make_event(DEVICE, kind=EventKind.TOOL_USE))  # seq 1, dropped
        led.append(make_event(DEVICE, kind=EventKind.TOOL_USE))  # seq 2, dropped
        led.append(_verdict_event(resource="tool:a", on_behalf_of=[]))  # seq 3
        led.append(_verdict_event(resource="tool:b", on_behalf_of=[]))  # seq 4
        entries = led.read_range(1, 4)
    finally:
        led.close()

    envelope = build_envelope(entries, agent=_agent(), device=_device(), window=_window())

    # The local ledger seqs for the two attested events are 3 and 4; the
    # envelope must not copy those — it renumbers fresh starting at 1.
    assert [e["seq"] for e in envelope["events"]] == [1, 2]


def test_window_boundaries_match_the_callers_seq_range_exactly(tmp_path):
    """build_envelope does not invent a second windowing scheme (spec §3c):
    it attests exactly the entries the caller hands it, which is expected to
    be ledger.read_range(checkpoint.seq_start, checkpoint.seq_end)."""
    led = Ledger(tmp_path / "ledger.db", DEVICE)
    try:
        led.append(_verdict_event(resource="tool:a", on_behalf_of=[]))  # seq 1
        led.append(_verdict_event(resource="tool:b", on_behalf_of=[]))  # seq 2 - checkpoint boundary
        led.append(_verdict_event(resource="tool:c", on_behalf_of=[]))  # seq 3 - next window
        checkpoint_seq_start, checkpoint_seq_end = 1, 2
        entries = led.read_range(checkpoint_seq_start, checkpoint_seq_end)
    finally:
        led.close()

    envelope = build_envelope(entries, agent=_agent(), device=_device(), window=_window())

    assert [e["resource"] for e in envelope["events"]] == ["tool:a", "tool:b"]


def test_payload_hash_is_recomputed_raw_hex_not_copied_from_local_ledger(tmp_path):
    """The local ledger's payload_hash carries this repo's own "sha256:"
    prefix; Coriqo expects raw hex. build_envelope must recompute, not copy."""
    led = Ledger(tmp_path / "ledger.db", DEVICE)
    try:
        led.append(_verdict_event(resource="tool:a", on_behalf_of=[]))
        entries = led.read_range(1, 1)
    finally:
        led.close()

    local_entry = entries[0]
    assert local_entry.event.payload_hash.startswith("sha256:")

    envelope = build_envelope(entries, agent=_agent(), device=_device(), window=_window())
    event = envelope["events"][0]

    assert not event["payload_hash"].startswith("sha256:")
    assert event["payload_hash"] == local_sha256_hex(canonicalize(event["payload"]))[len("sha256:"):]
    assert event["payload_hash"] != local_entry.event.payload_hash


def test_on_behalf_of_always_present_making_the_envelope_always_v2(tmp_path):
    """A non-delegated call's on_behalf_of is an empty list, per AIR-7b, but
    the key itself must still be present on every event: Coriqo's
    envelope_schema_version() detects v2 by key presence, not by value, and
    this runtime emits v2 always going forward (spec §3b)."""
    led = Ledger(tmp_path / "ledger.db", DEVICE)
    try:
        led.append(_verdict_event(resource="tool:a", on_behalf_of=[]))
        led.append(_verdict_event(resource="tool:b", on_behalf_of=["agent_A", "agent_B"]))
        entries = led.read_range(1, 2)
    finally:
        led.close()

    envelope = build_envelope(entries, agent=_agent(), device=_device(), window=_window())

    assert envelope["events"][0]["on_behalf_of"] == []
    assert envelope["events"][1]["on_behalf_of"] == ["agent_A", "agent_B"]
    assert all("on_behalf_of" in e for e in envelope["events"])


def test_chain_head_matches_compute_chain_head_over_the_produced_events(tmp_path):
    led = Ledger(tmp_path / "ledger.db", DEVICE)
    try:
        led.append(_verdict_event(resource="tool:a", on_behalf_of=["agent_A"]))
        led.append(_verdict_event(resource="tool:b", on_behalf_of=[]))
        entries = led.read_range(1, 2)
    finally:
        led.close()

    envelope = build_envelope(entries, agent=_agent(), device=_device(), window=_window())

    assert envelope["chain_head"] == compute_chain_head(envelope["events"])
    assert envelope["chain_head"] != GENESIS_PREV_HASH


def test_empty_window_produces_empty_events_and_genesis_chain_head(tmp_path):
    led = Ledger(tmp_path / "ledger.db", DEVICE)
    try:
        led.append(make_event(DEVICE, kind=EventKind.TOOL_USE))
        entries = led.read_range(1, 1)  # only a non-attestable kind in range
    finally:
        led.close()

    envelope = build_envelope(entries, agent=_agent(), device=_device(), window=_window())

    assert envelope["events"] == []
    assert envelope["chain_head"] == GENESIS_PREV_HASH


def test_envelope_top_level_shape(tmp_path):
    led = Ledger(tmp_path / "ledger.db", DEVICE)
    try:
        led.append(_verdict_event(resource="tool:a", on_behalf_of=[]))
        entries = led.read_range(1, 1)
    finally:
        led.close()

    envelope = build_envelope(
        entries, agent=_agent(), device=_device(), window=_window()
    )

    assert envelope["emitter"] == _device()
    assert envelope["subject"] == _agent()
    assert envelope["window"] == _window()
    assert set(envelope.keys()) == {"emitter", "subject", "window", "events", "chain_head"}

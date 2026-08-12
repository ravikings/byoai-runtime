"""End-to-end checkpoint shipping -> tenant epoch Merkle tree, against a real
MockCoriqo server over ``httpx.ASGITransport`` — the level-3 half of spec
section 6.2 (level 4, external anchoring, is not implemented anywhere yet).

Mirrors the structure of ``test_end_to_end_sync.py``: real client code
(``Checkpointer``, ``Shipper``) against the mock, which independently
re-verifies wire format and checkpoint signatures rather than trusting a
canned response.
"""

from __future__ import annotations

import time
import uuid

import pytest
from tests.recorder.conftest import asgi_client as _shared_asgi_client
from tests.recorder.mock_coriqo import MockCoriqo

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.checkpoint import Checkpointer
from byoai.recorder.enroll import enroll
from byoai.recorder.keys import DeviceKey, load_or_create_device_key
from byoai.recorder.ledger import Ledger
from byoai.recorder.merkle import verify_inclusion
from byoai.recorder.schema import (
    EVENT_SCHEMA_VERSION,
    AgentEvent,
    EventKind,
    new_span_id,
    new_trace_id,
)
from byoai.recorder.shipper import ShipError, Shipper

CORIQO_BASE_URL = "https://coriqo.example.com"


def make_event(device_id: str, *, i: int = 0) -> AgentEvent:
    payload = {"i": i}
    return AgentEvent(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id="evt_" + uuid.uuid4().hex,
        device_id=device_id,
        session_id="sess_1",
        seq=0,
        kind=EventKind.TOOL_USE.value,
        ts_device="2026-08-10T12:00:00.000000Z",
        ts_monotonic_ns=time.monotonic_ns(),
        tool_use_id="toolu_" + uuid.uuid4().hex[:8],
        tool_name="Bash",
        payload=payload,
        payload_hash=sha256_hex(canonicalize(payload)),
        model="claude-opus-4-20250514",
        provider="anthropic",
        trace_id=new_trace_id(),
        span_id=new_span_id(),
        parent_span_id=None,
        continues_from=None,
    )


def asgi_client(mock: MockCoriqo):
    return _shared_asgi_client(mock, base_url=CORIQO_BASE_URL)


def enroll_device(dir_path, mock: MockCoriqo, *, token: str = "cik_live_test"):
    dir_path.mkdir(parents=True, exist_ok=True)
    client = asgi_client(mock)
    try:
        state = enroll(
            coriqo_base_url=CORIQO_BASE_URL,
            token=token,
            key_dir=dir_path,
            http_client=client,
        )
    finally:
        client.close()
    key = load_or_create_device_key(dir_path)
    ledger = Ledger(dir_path / "ledger.sqlite3", state.device_id)
    return ledger, key, state.device_id


def emit_checkpoint(ledger: Ledger, key: DeviceKey, *, n_events: int) -> dict:
    """Append ``n_events`` and force a checkpoint over exactly those events."""
    checkpointer = Checkpointer(ledger, key, every_events=10_000, every_seconds=10_000.0)
    for i in range(n_events):
        entry = ledger.append(make_event(key.device_id, i=i))
        checkpointer.note(entry.seq)
    cp = checkpointer.flush()
    assert cp is not None
    return cp


# -- checkpoint shipping ------------------------------------------------------


def test_checkpoint_ships_and_is_stored_by_seq_end(tmp_path):
    mock = MockCoriqo()
    ledger, key, device_id = enroll_device(tmp_path, mock)
    try:
        emit_checkpoint(ledger, key, n_events=3)

        shipper = Shipper(
            ledger, key, coriqo_base_url=CORIQO_BASE_URL, http_client=asgi_client(mock)
        )
        try:
            result = shipper.ship_checkpoints_once()
            assert result is not None
            assert result.accepted == 1
            assert result.duplicates == 0
            assert result.synced_checkpoint_up_to == 3
            assert ledger.get_synced_checkpoint_up_to() == 3

            stored = mock.checkpoints_for(device_id)
            assert [c.seq_end for c in stored] == [3]

            # No-op re-ship: nothing unsynced.
            assert shipper.ship_checkpoints_once() is None
        finally:
            shipper.close()
    finally:
        ledger.close()


def test_checkpoint_reship_after_watermark_reset_dedupes(tmp_path):
    # Simulates a crash after the server accepted a batch but before the
    # client persisted the ack: the watermark rewinds, the same checkpoint
    # ships again, and the server must treat it as a duplicate, not a
    # second leaf.
    mock = MockCoriqo()
    ledger, key, device_id = enroll_device(tmp_path, mock)
    try:
        emit_checkpoint(ledger, key, n_events=2)
        shipper = Shipper(
            ledger, key, coriqo_base_url=CORIQO_BASE_URL, http_client=asgi_client(mock)
        )
        try:
            shipper.ship_checkpoints_once()

            with ledger._lock:  # noqa: SLF001 - test-only rewind of the watermark
                ledger._conn.execute(
                    "UPDATE sync_state SET synced_checkpoint_up_to_seq = 0 WHERE id = 1"
                )

            result = shipper.ship_checkpoints_once()
            assert result is not None
            assert result.accepted == 0
            assert result.duplicates == 1
            assert len(mock.checkpoints_for(device_id)) == 1
        finally:
            shipper.close()
    finally:
        ledger.close()


def test_tampered_checkpoint_signature_is_rejected(tmp_path, monkeypatch):
    mock = MockCoriqo()
    ledger, key, device_id = enroll_device(tmp_path, mock)
    try:
        emit_checkpoint(ledger, key, n_events=1)

        original_sign = DeviceKey.sign
        monkeypatch.setattr(
            DeviceKey, "sign", lambda self, data: original_sign(self, b"not-the-real-body")
        )

        shipper = Shipper(
            ledger, key, coriqo_base_url=CORIQO_BASE_URL, http_client=asgi_client(mock)
        )
        try:
            with pytest.raises(ShipError) as exc_info:
                shipper.ship_checkpoints_once()
            assert "401" in str(exc_info.value)
            assert mock.checkpoints_for(device_id) == []
            assert ledger.get_synced_checkpoint_up_to() == 0
        finally:
            shipper.close()
    finally:
        ledger.close()


# -- epoch tree ---------------------------------------------------------------


def test_build_epoch_is_none_when_nothing_pending():
    mock = MockCoriqo()
    assert mock.build_epoch() is None


def test_build_epoch_over_two_devices_gives_each_a_verifiable_proof(tmp_path):
    mock = MockCoriqo(valid_tokens={"tok_a", "tok_b"})

    ledger_a, key_a, dev_a = enroll_device(tmp_path / "a", mock, token="tok_a")
    ledger_b, key_b, dev_b = enroll_device(tmp_path / "b", mock, token="tok_b")

    try:
        cp_a = emit_checkpoint(ledger_a, key_a, n_events=2)
        cp_b = emit_checkpoint(ledger_b, key_b, n_events=3)

        shipper_a = Shipper(
            ledger_a, key_a, coriqo_base_url=CORIQO_BASE_URL, http_client=asgi_client(mock)
        )
        shipper_b = Shipper(
            ledger_b, key_b, coriqo_base_url=CORIQO_BASE_URL, http_client=asgi_client(mock)
        )
        try:
            shipper_a.ship_checkpoints_once()
            shipper_b.ship_checkpoints_once()

            epoch = mock.build_epoch()
            assert epoch is not None
            assert len(epoch.checkpoint_keys) == 2
            assert set(epoch.checkpoint_keys) == {
                (dev_a, cp_a["seq_end"]),
                (dev_b, cp_b["seq_end"]),
            }

            proof_a = epoch.proof_for(dev_a, cp_a["seq_end"])
            proof_b = epoch.proof_for(dev_b, cp_b["seq_end"])
            assert proof_a is not None and proof_b is not None
            assert verify_inclusion(proof_a)
            assert verify_inclusion(proof_b)
            assert proof_a.root == epoch.root
            assert proof_b.root == epoch.root

            # A checkpoint that was never shipped has no proof in this epoch.
            assert epoch.proof_for(dev_a, 999) is None

            # A second call with nothing new pending finds nothing to build.
            assert mock.build_epoch() is None
        finally:
            shipper_a.close()
            shipper_b.close()
    finally:
        ledger_a.close()
        ledger_b.close()


def test_epochs_do_not_reuse_already_epoched_checkpoints(tmp_path):
    # A checkpoint that shipped before an epoch closed must not also appear
    # as a leaf in the *next* epoch — an inclusion proof would then be
    # ambiguous about which root it belongs under.
    mock = MockCoriqo()
    ledger, key, device_id = enroll_device(tmp_path, mock)
    try:
        cp1 = emit_checkpoint(ledger, key, n_events=1)
        shipper = Shipper(
            ledger, key, coriqo_base_url=CORIQO_BASE_URL, http_client=asgi_client(mock)
        )
        try:
            shipper.ship_checkpoints_once()
            first_epoch = mock.build_epoch()
            assert first_epoch is not None
            assert first_epoch.checkpoint_keys == [(device_id, cp1["seq_end"])]

            # A second checkpoint ships after the first epoch closed.
            checkpointer2 = Checkpointer(ledger, key, every_events=10_000, every_seconds=10_000.0)
            entry = ledger.append(make_event(device_id, i=99))
            checkpointer2.note(entry.seq)
            cp2 = checkpointer2.flush()
            shipper.ship_checkpoints_once()

            second_epoch = mock.build_epoch()
            assert second_epoch is not None
            assert second_epoch.checkpoint_keys == [(device_id, cp2["seq_end"])]
            assert second_epoch.root != first_epoch.root
        finally:
            shipper.close()
    finally:
        ledger.close()

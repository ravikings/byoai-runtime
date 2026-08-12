"""Tests for device key rotation and revocation (spec §8.4).

Uses the real :class:`Ledger` and :func:`rotate_key` — unlike
``test_verify.py``, this module exercises the writer, not just the
verifier, so a hand-built sqlite fixture would just be redundant.
"""

from __future__ import annotations

import base64

import pytest

from byoai.recorder.canonical import canonicalize
from byoai.recorder.keys import (
    PRIVATE_KEY_FILENAME,
    PUBLIC_KEY_FILENAME,
    load_or_create_device_key,
)
from byoai.recorder.ledger import Ledger
from byoai.recorder.rotation import rotate_key
from byoai.recorder.schema import (
    EVENT_SCHEMA_VERSION,
    AgentEvent,
    EventKind,
    new_event_id,
    new_span_id,
    new_trace_id,
    now_monotonic_ns,
    now_ts_device,
)
from byoai.recorder.verify import verify_ledger


def _make_ledger(tmp_path, name="ledger.db"):
    key_dir = tmp_path / "keys"
    old_key = load_or_create_device_key(key_dir)
    ledger = Ledger(tmp_path / name, device_id=old_key.device_id)
    return key_dir, old_key, ledger


def _append_event(ledger: Ledger, device_id: str, kind: str, session_id: str = "ses_1") -> None:
    event = AgentEvent(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id=new_event_id(),
        device_id=device_id,
        session_id=session_id,
        seq=0,
        kind=kind,
        ts_device=now_ts_device(),
        ts_monotonic_ns=now_monotonic_ns(),
        tool_use_id=None,
        tool_name=None,
        payload={"n": 1},
        payload_hash="sha256:" + "00" * 32,
        model=None,
        provider="anthropic",
        trace_id=new_trace_id(),
        span_id=new_span_id(),
        parent_span_id=None,
        continues_from=None,
    )
    ledger.append(event)


def test_rotate_key_produces_a_different_key_on_disk(tmp_path):
    key_dir, old_key, ledger = _make_ledger(tmp_path)
    try:
        old_priv_bytes = (key_dir / PRIVATE_KEY_FILENAME).read_bytes()
        old_pub_text = (key_dir / PUBLIC_KEY_FILENAME).read_text()

        new_key = rotate_key(key_dir, ledger, reason="rotation")

        assert new_key.device_id != old_key.device_id
        assert new_key.public_key_b64 != old_key.public_key_b64

        new_priv_bytes = (key_dir / PRIVATE_KEY_FILENAME).read_bytes()
        new_pub_text = (key_dir / PUBLIC_KEY_FILENAME).read_text()
        assert new_priv_bytes != old_priv_bytes
        assert new_pub_text.strip() != old_pub_text.strip()

        # Loading from key_dir now yields the new key, not the old one.
        reloaded = load_or_create_device_key(key_dir)
        assert reloaded.device_id == new_key.device_id
    finally:
        ledger.close()


def test_key_rotated_event_has_verifiable_cross_signature(tmp_path):
    key_dir, old_key, ledger = _make_ledger(tmp_path)
    try:
        new_key = rotate_key(key_dir, ledger, reason="rotation")
    finally:
        ledger.close()

    report = verify_ledger(
        tmp_path / "ledger.db",
        device_public_keys={old_key.device_id: old_key.public_key_b64},
    )
    assert len(report.key_rotations) == 1
    rotation = report.key_rotations[0]
    assert rotation["old_device_id"] == old_key.device_id
    assert rotation["new_device_id"] == new_key.device_id
    assert rotation["cross_signature_verified"] is True
    assert report.ok is True


def test_entries_after_rotation_use_the_new_key(tmp_path):
    key_dir, old_key, ledger = _make_ledger(tmp_path)
    try:
        _append_event(ledger, old_key.device_id, EventKind.MESSAGE.value)
        new_key = rotate_key(key_dir, ledger, reason="rotation")
        # Ledger object itself doesn't auto-switch device_id; a real Recorder
        # would reopen/reassign, but appends after rotation should be made
        # under the new identity.
        ledger.device_id = new_key.device_id
        _append_event(ledger, new_key.device_id, EventKind.MESSAGE.value)
    finally:
        ledger.close()

    report = verify_ledger(
        tmp_path / "ledger.db",
        device_public_keys={old_key.device_id: old_key.public_key_b64},
    )
    assert old_key.device_id in report.device_ids
    assert new_key.device_id in report.device_ids
    assert report.ok is True
    assert report.stale_key_usage == []


def test_verify_ledger_does_not_flag_rotation_boundary_as_broken_chain(tmp_path):
    """The whole point of cross-signing: a device_id change right after
    KEY_ROTATED must never be reported as chain tampering."""
    key_dir, old_key, ledger = _make_ledger(tmp_path)
    try:
        _append_event(ledger, old_key.device_id, EventKind.SESSION_START.value)
        new_key = rotate_key(key_dir, ledger, reason="rotation")
        ledger.device_id = new_key.device_id
        _append_event(ledger, new_key.device_id, EventKind.MESSAGE.value)
        _append_event(ledger, new_key.device_id, EventKind.MESSAGE.value)
    finally:
        ledger.close()

    report = verify_ledger(
        tmp_path / "ledger.db",
        device_public_keys={old_key.device_id: old_key.public_key_b64},
    )
    assert report.broken_links == []
    assert report.gaps == []
    assert report.stale_key_usage == []
    assert report.ok is True


def test_verify_ledger_flags_forged_cross_signature(tmp_path):
    key_dir, old_key, ledger = _make_ledger(tmp_path)
    try:
        rotate_key(key_dir, ledger, reason="rotation")
    finally:
        ledger.close()

    # Tamper the cross_signature stored in the ledger row directly.
    import json
    import sqlite3

    conn = sqlite3.connect(tmp_path / "ledger.db")
    row = conn.execute(
        "SELECT seq, payload FROM agent_events WHERE kind = ?", (EventKind.KEY_ROTATED.value,)
    ).fetchone()
    seq, payload_json = row
    payload = json.loads(payload_json)
    payload["cross_signature"] = "ed25519:" + base64.b64encode(b"\x00" * 64).decode()
    conn.execute(
        "UPDATE agent_events SET payload = ? WHERE seq = ?", (json.dumps(payload), seq)
    )
    conn.commit()
    conn.close()

    report = verify_ledger(
        tmp_path / "ledger.db",
        device_public_keys={old_key.device_id: old_key.public_key_b64},
    )
    # payload_hash / entry_hash no longer match the tampered payload, so this
    # is caught as a broken link (tamper-evidence) *and* the cross-signature
    # itself must not verify.
    assert report.ok is False
    rotation = report.key_rotations[0]
    assert rotation["cross_signature_verified"] is False


def test_verify_ledger_flags_forged_new_public_key_without_breaking_chain_detection(tmp_path):
    """A forged new_public_key alone (chain hash consistent because we also
    patch payload_hash/entry_hash to match) must still fail cross-sig check."""
    key_dir, old_key, ledger = _make_ledger(tmp_path)
    try:
        rotate_key(key_dir, ledger, reason="rotation")
    finally:
        ledger.close()

    bogus_pub = base64.b64encode(b"\x01" * 32).decode()

    import json
    import sqlite3

    from byoai.recorder.canonical import sha256_hex

    conn = sqlite3.connect(tmp_path / "ledger.db")
    row = conn.execute(
        "SELECT seq, payload FROM agent_events WHERE kind = ?", (EventKind.KEY_ROTATED.value,)
    ).fetchone()
    seq, payload_json = row
    payload = json.loads(payload_json)
    payload["new_public_key"] = bogus_pub
    new_payload_hash = sha256_hex(canonicalize(payload))
    conn.execute(
        "UPDATE agent_events SET payload = ?, payload_hash = ? WHERE seq = ?",
        (json.dumps(payload), new_payload_hash, seq),
    )
    conn.commit()
    conn.close()

    report = verify_ledger(
        tmp_path / "ledger.db",
        device_public_keys={old_key.device_id: old_key.public_key_b64},
    )
    rotation = report.key_rotations[0]
    assert rotation["cross_signature_verified"] is False
    assert report.ok is False


def test_revocation_reason_and_effective_epoch_recorded(tmp_path):
    key_dir, old_key, ledger = _make_ledger(tmp_path)
    try:
        new_key = rotate_key(key_dir, ledger, reason="revocation")
    finally:
        ledger.close()

    report = verify_ledger(
        tmp_path / "ledger.db",
        device_public_keys={old_key.device_id: old_key.public_key_b64},
    )
    rotation = report.key_rotations[0]
    assert rotation["reason"] == "revocation"
    assert rotation["effective_epoch"]
    assert rotation["new_device_id"] == new_key.device_id


def test_rotate_key_rejects_unknown_reason(tmp_path):
    key_dir, old_key, ledger = _make_ledger(tmp_path)
    try:
        with pytest.raises(ValueError):
            rotate_key(key_dir, ledger, reason="not_a_real_reason")
    finally:
        ledger.close()


def test_rotation_without_pubkey_is_unchecked_not_failed(tmp_path):
    key_dir, old_key, ledger = _make_ledger(tmp_path)
    try:
        rotate_key(key_dir, ledger, reason="rotation")
    finally:
        ledger.close()

    report = verify_ledger(tmp_path / "ledger.db")
    rotation = report.key_rotations[0]
    assert rotation["cross_signature_verified"] is None
    assert report.ok is True
    assert any("NOT checked" in note for note in report.notes)


def test_rotate_cli(tmp_path, capsys):
    from byoai.recorder.rotation import rotate_cli

    key_dir = tmp_path / "keys"
    old_key = load_or_create_device_key(key_dir)
    ledger_path = tmp_path / "device.db"
    ledger = Ledger(ledger_path, device_id=old_key.device_id)
    ledger.close()

    rc = rotate_cli(
        ["--key-dir", str(key_dir), "--ledger", str(ledger_path), "--reason", "compromise"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert old_key.device_id in out
    assert "->" in out

    new_key = load_or_create_device_key(key_dir)
    assert new_key.device_id != old_key.device_id

    report = verify_ledger(
        ledger_path, device_public_keys={old_key.device_id: old_key.public_key_b64}
    )
    assert report.key_rotations[0]["reason"] == "compromise"
    assert report.key_rotations[0]["cross_signature_verified"] is True

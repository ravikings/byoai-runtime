"""End-to-end enroll -> ship round trip against a real MockCoriqo server —
workstream I.

Unlike the pure ``httpx.MockTransport`` unit tests in ``test_shipper.py`` and
``test_enroll.py`` (which hand-craft the expected server response), this file
wires real :class:`~byoai.recorder.enroll` /
:class:`~byoai.recorder.shipper.Shipper` client code against
:class:`tests.recorder.mock_coriqo.MockCoriqo` over ``httpx.ASGITransport`` —
no real sockets, no canned responses. The mock server independently
re-derives and verifies everything the client puts on the wire (signature
over the exact canonical bytes received, gzip framing, header names, status
codes), so a client/server wire-format mismatch shows up as a real HTTP
failure here instead of silently passing a fake.
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest
from tests.recorder.conftest import asgi_client as _shared_asgi_client
from tests.recorder.mock_coriqo import MockCoriqo

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.enroll import (
    EnrollmentError,
    EnrollmentState,
    enroll,
    load_enrollment_state,
)
from byoai.recorder.keys import DeviceKey, load_or_create_device_key
from byoai.recorder.ledger import Ledger
from byoai.recorder.schema import EVENT_SCHEMA_VERSION, AgentEvent, EventKind
from byoai.recorder.shipper import ShipError, Shipper
from byoai.recorder.verify import verify_ledger

CORIQO_BASE_URL = "https://coriqo.example.com"


def make_event(
    device_id: str, *, session_id: str = "sess_1", payload: dict | None = None
) -> AgentEvent:
    payload = {"command": "ls -la"} if payload is None else payload
    return AgentEvent(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id="evt_" + uuid.uuid4().hex,
        device_id=device_id,
        session_id=session_id,
        seq=0,  # placeholder; the ledger assigns the real seq
        kind=EventKind.TOOL_USE.value,
        ts_device="2026-08-10T12:00:00.000000Z",
        ts_monotonic_ns=time.monotonic_ns(),
        tool_use_id="toolu_" + uuid.uuid4().hex[:8],
        tool_name="Bash",
        payload=payload,
        payload_hash=sha256_hex(canonicalize(payload)),
        model="claude-opus-4-20250514",
        provider="anthropic",
    )


def asgi_client(mock: MockCoriqo) -> httpx.Client:
    return _shared_asgi_client(mock, base_url=CORIQO_BASE_URL)


def do_enroll(tmp_path, mock: MockCoriqo, *, token: str = "cik_live_test") -> EnrollmentState:
    client = asgi_client(mock)
    try:
        return enroll(
            coriqo_base_url=CORIQO_BASE_URL,
            token=token,
            key_dir=tmp_path,
            http_client=client,
        )
    finally:
        client.close()


# -- 1. enroll --------------------------------------------------------------


def test_enroll_persists_state_and_registers_device(tmp_path):
    mock = MockCoriqo()

    state = do_enroll(tmp_path, mock)

    assert state.device_id
    assert state.coriqo_base_url

    persisted = load_enrollment_state(tmp_path)
    assert persisted == state

    devices = mock.devices()
    assert state.device_id in devices
    key = load_or_create_device_key(tmp_path)
    assert devices[state.device_id].public_key_b64 == key.public_key_b64


def test_enroll_rejects_unknown_token(tmp_path):
    mock = MockCoriqo()
    client = asgi_client(mock)
    try:
        with pytest.raises(EnrollmentError):
            enroll(
                coriqo_base_url=CORIQO_BASE_URL,
                token="cik_live_bogus",
                key_dir=tmp_path,
                http_client=client,
            )
    finally:
        client.close()
    assert mock.devices() == {}


# -- 2/3. ship, verify, no-op re-ship ---------------------------------------


def test_ship_verify_and_noop_reship(tmp_path):
    mock = MockCoriqo()
    state = do_enroll(tmp_path, mock)
    device_id = state.device_id
    key = load_or_create_device_key(tmp_path)

    ledger = Ledger(tmp_path / "ledger.sqlite3", device_id)
    ship_client = asgi_client(mock)
    shipper = Shipper(
        ledger,
        key,
        coriqo_base_url=CORIQO_BASE_URL,
        http_client=ship_client,
    )
    try:
        for i in range(3):
            ledger.append(make_event(device_id, payload={"i": i}))

        result = shipper.ship_once()

        assert result is not None
        assert result.accepted == 3
        assert result.duplicates == 0
        assert result.gaps == []
        assert result.synced_up_to == 3
        assert ledger.get_synced_up_to() == 3

        server_entries = mock.entries_for(device_id)
        assert [e.seq for e in server_entries] == [1, 2, 3]

        report = verify_ledger(tmp_path / "ledger.sqlite3")
        assert report.ok is True

        # 3. Nothing unsynced -> no-op, no duplicate POST.
        noop_result = shipper.ship_once()
        assert noop_result is None
        assert [e.seq for e in mock.entries_for(device_id)] == [1, 2, 3]
    finally:
        shipper.close()
        ledger.close()


# -- 4. real gap scenario -----------------------------------------------


def test_ship_with_real_gap_response_stalls_watermark_before_gap(tmp_path):
    mock = MockCoriqo()
    state = do_enroll(tmp_path, mock)
    device_id = state.device_id
    key = load_or_create_device_key(tmp_path)

    ledger = Ledger(tmp_path / "ledger.sqlite3", device_id)
    ship_client = asgi_client(mock)
    shipper = Shipper(
        ledger,
        key,
        coriqo_base_url=CORIQO_BASE_URL,
        http_client=ship_client,
    )
    try:
        for i in range(5):
            ledger.append(make_event(device_id, payload={"i": i}))

        # Pre-seed the mock's gap state (its documented test-introspection
        # surface) so the ingest response the shipper receives is a real
        # 202 whose `gaps` naturally contains this seq, rather than a
        # hand-crafted MockTransport response.
        mock.inject_gap(device_id, 3)

        result = shipper.ship_once()

        assert result is not None
        assert result.gaps == [[3, 3]]
        # All 5 entries were accepted server-side (the mock still stores
        # everything; gaps is orthogonal to acceptance)...
        assert [e.seq for e in mock.entries_for(device_id)] == [1, 2, 3, 4, 5]
        # ...but the client must never mark the gapped seq (or anything
        # shipped after it) as synced.
        assert result.synced_up_to == 2
        assert ledger.get_synced_up_to() == 2

        # A follow-up ship re-sends the still-unsynced tail (3, 4, 5); the
        # gap is permanent so the watermark stays pinned at 2.
        second = shipper.ship_once()
        assert second is not None
        assert second.duplicates == 3
        assert second.gaps == [[3, 3]]
        assert second.synced_up_to == 2
        assert ledger.get_synced_up_to() == 2
    finally:
        shipper.close()
        ledger.close()


# -- 5. signature tampering ---------------------------------------------


def test_tampered_signature_is_rejected_and_watermark_holds(tmp_path, monkeypatch):
    mock = MockCoriqo()
    state = do_enroll(tmp_path, mock)
    device_id = state.device_id
    key = load_or_create_device_key(tmp_path)

    ledger = Ledger(tmp_path / "ledger.sqlite3", device_id)
    ship_client = asgi_client(mock)
    shipper = Shipper(
        ledger,
        key,
        coriqo_base_url=CORIQO_BASE_URL,
        http_client=ship_client,
    )
    try:
        ledger.append(make_event(device_id, payload={"i": 0}))

        # Sign the wrong bytes so the mock's independent re-derivation of
        # the signature over the exact canonical bytes it received fails.
        # DeviceKey uses __slots__, so the instance can't take an ad-hoc
        # attribute — patch the unbound method on the class instead.
        original_sign = DeviceKey.sign
        monkeypatch.setattr(
            DeviceKey, "sign", lambda self, data: original_sign(self, b"not-the-real-body")
        )

        with pytest.raises(ShipError) as exc_info:
            shipper.ship_once()
        assert "401" in str(exc_info.value)

        assert mock.entries_for(device_id) == []
        assert ledger.get_synced_up_to() == 0
    finally:
        shipper.close()
        ledger.close()

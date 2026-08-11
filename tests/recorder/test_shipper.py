"""Batching shipper tests — workstream G."""

from __future__ import annotations

import gzip
import json
import threading
import time
import uuid

import httpx
import pytest

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.keys import DeviceKey, load_or_create_device_key
from byoai.recorder.ledger import Ledger
from byoai.recorder.schema import EVENT_SCHEMA_VERSION, AgentEvent, EventKind
from byoai.recorder.shipper import ShipError, Shipper, ShipResult

DEVICE = "dev_test"


def make_event(session_id: str = "sess_1", *, payload: dict | None = None) -> AgentEvent:
    payload = {"command": "ls -la"} if payload is None else payload
    return AgentEvent(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id="evt_" + uuid.uuid4().hex,
        device_id=DEVICE,
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


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite3", DEVICE)
    yield led
    led.close()


@pytest.fixture
def key(tmp_path) -> DeviceKey:
    return load_or_create_device_key(tmp_path / "keydir")


def _decode_body(request: httpx.Request) -> dict:
    return json.loads(gzip.decompress(request.content))


def make_shipper(ledger: Ledger, key: DeviceKey, handler, **kwargs) -> Shipper:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Shipper(
        ledger,
        key,
        coriqo_base_url="https://coriqo.example.com",
        http_client=client,
        **kwargs,
    )


def test_ship_once_returns_none_when_nothing_unsynced(ledger, key):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called when nothing is unsynced")

    shipper = make_shipper(ledger, key, handler)
    assert shipper.ship_once() is None


def test_ship_once_clean_accept_advances_watermark(ledger, key):
    for i in range(3):
        ledger.append(make_event(payload={"i": i}))

    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        body = _decode_body(request)
        assert set(body.keys()) == {"device_id", "entries"}
        assert body["device_id"] == key.device_id
        assert len(body["entries"]) == 3
        for entry in body["entries"]:
            assert set(entry.keys()) == {"seq", "entry_hash", "event"}
        assert request.headers["x-coriqo-device"] == key.device_id
        assert request.headers["x-coriqo-signature"].startswith("ed25519:")
        assert request.headers["content-encoding"] == "gzip"
        return httpx.Response(202, json={"accepted": 3, "duplicates": 0, "gaps": []})

    shipper = make_shipper(ledger, key, handler)
    result = shipper.ship_once()

    assert len(requests_seen) == 1
    assert isinstance(result, ShipResult)
    assert result.accepted == 3
    assert result.duplicates == 0
    assert result.gaps == []
    assert result.synced_up_to == 3
    assert ledger.get_synced_up_to() == 3


def test_ship_once_partial_dedup(ledger, key):
    for i in range(4):
        ledger.append(make_event(payload={"i": i}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"accepted": 2, "duplicates": 2, "gaps": []})

    shipper = make_shipper(ledger, key, handler)
    result = shipper.ship_once()

    assert result.accepted == 2
    assert result.duplicates == 2
    # Even though only 2 were newly accepted, all 4 shipped seqs are
    # confirmed by the server (accepted + duplicates), so the watermark
    # advances past all of them.
    assert result.synced_up_to == 4
    assert ledger.get_synced_up_to() == 4


def test_ship_once_signature_is_over_canonicalized_body(ledger, key):
    ledger.append(make_event())
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _decode_body(request)
        captured["signature"] = request.headers["x-coriqo-signature"]
        return httpx.Response(202, json={"accepted": 1, "duplicates": 0, "gaps": []})

    shipper = make_shipper(ledger, key, handler)
    shipper.ship_once()

    expected_sig = key.sign(canonicalize(captured["body"]))
    assert captured["signature"] == expected_sig


def test_ship_once_gap_in_middle_does_not_advance_past_gap(ledger, key):
    """Critical correctness case: a gap reported mid-batch must stop the
    watermark before the gap, even though later seqs in the same batch were
    reported accepted by the server."""
    for i in range(5):
        ledger.append(make_event(payload={"i": i}))

    # Server accepts seq 1,2 and 4,5 but reports seq 3 as a gap (e.g. a
    # different device's concurrent write raced it, or local drop). Even
    # though seq 4 and 5 are "accepted" per the count, the watermark may not
    # skip over the gap at seq 3.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202, json={"accepted": 4, "duplicates": 0, "gaps": [[3, 3]]}
        )

    shipper = make_shipper(ledger, key, handler)
    result = shipper.ship_once()

    assert result.gaps == [[3, 3]]
    # Watermark stops at seq 2 (the highest seq strictly before the gap),
    # never advancing to 4 or 5 despite them being in the "accepted" count.
    assert result.synced_up_to == 2
    assert ledger.get_synced_up_to() == 2

    # The next read_unsynced() call still includes seq 3 onward, so a
    # subsequent ship attempt will retry them.
    unsynced_seqs = [e.seq for e in ledger.read_unsynced()]
    assert unsynced_seqs == [3, 4, 5]


def test_ship_once_stale_gap_below_watermark_does_not_stall_sync(ledger, key):
    # A gap the server reports at a seq already behind the local watermark
    # (an old, unrelated, or permanently-lost gap from before this batch)
    # must never block this batch — or every batch after it — from syncing.
    for _ in range(3):
        ledger.append(make_event())
    ledger.set_synced_up_to(2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"accepted": 1, "duplicates": 0, "gaps": [[1, 1]]})

    shipper = make_shipper(ledger, key, handler)
    result = shipper.ship_once()

    assert result.synced_up_to == 3
    assert ledger.get_synced_up_to() == 3


def test_ship_once_no_confirmation_leaves_watermark_unchanged(ledger, key):
    ledger.append(make_event())

    def handler(request: httpx.Request) -> httpx.Response:
        # Entire batch is one big gap: nothing safe to mark synced.
        return httpx.Response(202, json={"accepted": 0, "duplicates": 0, "gaps": [[1, 1]]})

    shipper = make_shipper(ledger, key, handler)
    result = shipper.ship_once()

    assert result.synced_up_to == 0
    assert ledger.get_synced_up_to() == 0


def test_ship_once_raises_ship_error_on_non_2xx(ledger, key):
    ledger.append(make_event())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    shipper = make_shipper(ledger, key, handler)
    with pytest.raises(ShipError):
        shipper.ship_once()

    # Nothing was confirmed; watermark untouched.
    assert ledger.get_synced_up_to() == 0


def test_ship_once_raises_ship_error_on_network_failure(ledger, key):
    ledger.append(make_event())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    shipper = make_shipper(ledger, key, handler)
    with pytest.raises(ShipError):
        shipper.ship_once()


def test_ship_once_respects_max_batch_events(ledger, key):
    for i in range(10):
        ledger.append(make_event(payload={"i": i}))

    def handler(request: httpx.Request) -> httpx.Response:
        body = _decode_body(request)
        assert len(body["entries"]) == 3
        return httpx.Response(202, json={"accepted": 3, "duplicates": 0, "gaps": []})

    shipper = make_shipper(ledger, key, handler, max_batch_events=3)
    result = shipper.ship_once()
    assert result.synced_up_to == 3
    assert len(ledger.read_unsynced()) == 7


def test_ship_once_respects_max_batch_bytes(ledger, key):
    # Each entry's canonical JSON is a few hundred bytes; force a tiny byte
    # cap so only the first entry fits.
    for i in range(5):
        ledger.append(make_event(payload={"i": i, "padding": "x" * 50}))

    def handler(request: httpx.Request) -> httpx.Response:
        body = _decode_body(request)
        assert len(body["entries"]) == 1
        return httpx.Response(202, json={"accepted": 1, "duplicates": 0, "gaps": []})

    shipper = make_shipper(ledger, key, handler, max_batch_bytes=200)
    shipper.ship_once()


def test_retry_after_overrides_backoff_and_run_forever_waits_at_least_that_long(ledger, key):
    ledger.append(make_event())
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "7"})
        return httpx.Response(202, json={"accepted": 1, "duplicates": 0, "gaps": []})

    waits: list[float] = []
    stop = threading.Event()

    def fake_wait(seconds: float) -> bool:
        waits.append(seconds)
        # Stop after we've observed the backoff wait following the failure.
        if len(waits) >= 2:
            stop.set()
        return stop.is_set()

    shipper = make_shipper(ledger, key, handler, wait=fake_wait)
    start = time.monotonic()
    shipper.run_forever(stop=stop)
    elapsed = time.monotonic() - start

    # Fast: the injected wait function never actually sleeps for 7 seconds.
    assert elapsed < 0.5
    assert call_count >= 1
    # The wait triggered by the 429/Retry-After must be exactly 7 (not the
    # computed exponential-backoff-with-jitter value).
    assert 7 in waits


def test_run_forever_exits_promptly_when_stop_is_set_mid_backoff(ledger, key):
    ledger.append(make_event())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    stop = threading.Event()
    call_count = 0

    def fake_wait(seconds: float) -> bool:
        nonlocal call_count
        call_count += 1
        # Simulate stop firing while we're "asleep" mid-backoff.
        stop.set()
        return True

    shipper = make_shipper(ledger, key, handler, wait=fake_wait)

    thread = threading.Thread(target=shipper.run_forever, kwargs={"stop": stop})
    start = time.monotonic()
    thread.start()
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - start

    assert not thread.is_alive()
    assert elapsed < 2.0
    assert call_count >= 1


def test_run_forever_never_raises_on_repeated_failures(ledger, key):
    ledger.append(make_event())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    stop = threading.Event()
    attempts = 0

    def fake_wait(seconds: float) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            stop.set()
        return stop.is_set()

    shipper = make_shipper(ledger, key, handler, wait=fake_wait)
    # Must not raise despite every attempt failing.
    shipper.run_forever(stop=stop)
    assert attempts >= 3

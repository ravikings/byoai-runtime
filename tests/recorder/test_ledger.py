"""Tests for the hash-chained device ledger.

The bar here is adversarial, not happy-path: the ledger's only value is that
someone who edits the SQLite file afterwards gets caught, and that a crash
resumes the same chain instead of quietly starting a new one.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.ledger import (
    GENESIS_PREV_HASH,
    Ledger,
    LedgerWriteError,
    compute_entry_hash,
)
from byoai.recorder.schema import (
    AgentEvent,
    EventKind,
    event_digest,
)

from .conftest import make_event as _make_event

DEVICE = "dev_test"


def make_event(
    session_id: str = "sess_1",
    kind: EventKind = EventKind.TOOL_USE,
    *,
    payload: dict | None = None,
    tool_use_id: str | None = None,
    tool_name: str | None = "Bash",
) -> AgentEvent:
    """This file's events are all for the single fixed device ``DEVICE`` —
    everything else is the shared shape in ``conftest.make_event``."""
    return _make_event(
        DEVICE,
        session_id,
        kind,
        payload=payload,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
    )


def first_broken_seq(path) -> int | None:
    """Recompute the chain straight off disk; return the first bad seq.

    Deliberately independent of the Ledger object's in-memory state — this is
    what an offline examiner with only the file would do.
    """
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT * FROM agent_events ORDER BY seq").fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM agent_events LIMIT 0").description]
    finally:
        conn.close()

    prev = GENESIS_PREV_HASH
    for raw in rows:
        row = dict(zip(cols, raw, strict=True))
        event = AgentEvent(
            schema_version=row["schema_version"],
            event_id=row["event_id"],
            device_id=row["device_id"],
            session_id=row["session_id"],
            seq=row["seq"],
            kind=row["kind"],
            ts_device=row["ts_device"],
            ts_monotonic_ns=row["ts_monotonic_ns"],
            tool_use_id=row["tool_use_id"],
            tool_name=row["tool_name"],
            payload=json.loads(row["payload"]),
            payload_hash=row["payload_hash"],
            model=row["model"],
            provider=row["provider"],
            trace_id=row.get("trace_id") or "",
            span_id=row.get("span_id") or "",
            parent_span_id=row.get("parent_span_id"),
            continues_from=row.get("continues_from"),
        )
        digest = event_digest(event)
        expected = compute_entry_hash(prev, row["seq"], digest)
        if (
            digest != row["event_digest"]
            or row["prev_hash"] != prev
            or row["entry_hash"] != expected
        ):
            return row["seq"]
        prev = row["entry_hash"]
    return None


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "nested" / "ledger.db"


def test_empty_ledger_starts_at_genesis(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        assert led.head == GENESIS_PREV_HASH
        assert led.next_seq == 1
        assert led.missing_ranges() == []
        assert list(led.iter_entries()) == []
    finally:
        led.close()


def test_append_assigns_seq_and_links_chain(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        entries = [led.append(make_event()) for _ in range(5)]
        assert [e.seq for e in entries] == [1, 2, 3, 4, 5]
        assert entries[0].prev_hash == GENESIS_PREV_HASH
        for prev, cur in zip(entries, entries[1:], strict=False):
            assert cur.prev_hash == prev.entry_hash
            assert cur.entry_hash == compute_entry_hash(
                cur.prev_hash, cur.seq, cur.event_digest
            )
        assert led.head == entries[-1].entry_hash
        assert led.next_seq == 6
        assert first_broken_seq(ledger_path) is None
    finally:
        led.close()


def test_append_uses_wal_and_normal_sync(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        conn = sqlite3.connect(str(ledger_path))
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            conn.close()
        # synchronous is per-connection, so check the ledger's own connection.
        assert led._conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        led.close()


def test_event_roundtrips_through_read_range(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        payload = {"command": "rm -rf /tmp/x", "nested": {"b": 2, "a": [1, "two"]}}
        original = make_event(payload=payload, tool_use_id="toolu_abc", tool_name="Bash")
        written = led.append(original)
        (fetched,) = led.read_range(1, 1)
        assert fetched.seq == written.seq == 1
        assert fetched.event.payload == payload
        assert fetched.event.event_id == original.event_id
        assert fetched.event.tool_use_id == "toolu_abc"
        assert fetched.event.model == original.model
        assert fetched.event.provider == original.provider
        assert event_digest(fetched.event) == written.event_digest
    finally:
        led.close()


def test_read_range_is_inclusive_and_iter_matches(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        for _ in range(6):
            led.append(make_event())
        assert [e.seq for e in led.read_range(2, 4)] == [2, 3, 4]
        assert [e.seq for e in led.iter_entries()] == [1, 2, 3, 4, 5, 6]
    finally:
        led.close()


def test_tamper_with_payload_breaks_chain_at_that_seq(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        for _ in range(5):
            led.append(make_event())
    finally:
        led.close()

    assert first_broken_seq(ledger_path) is None

    # An examiner-hostile edit: rewrite what the agent actually ran, in place,
    # leaving every hash column untouched.
    conn = sqlite3.connect(str(ledger_path))
    try:
        conn.execute(
            "UPDATE agent_events SET payload = ? WHERE seq = 3",
            (json.dumps({"command": "echo harmless"}),),
        )
        conn.commit()
    finally:
        conn.close()

    assert first_broken_seq(ledger_path) == 3


def test_tamper_with_entry_hash_breaks_chain_at_that_seq(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        for _ in range(4):
            led.append(make_event())
    finally:
        led.close()

    conn = sqlite3.connect(str(ledger_path))
    try:
        conn.execute(
            "UPDATE agent_events SET entry_hash = ? WHERE seq = 2",
            ("sha256:" + "ff" * 32,),
        )
        conn.commit()
    finally:
        conn.close()

    # seq 2's own link no longer matches, so that is where it breaks.
    assert first_broken_seq(ledger_path) == 2


def test_deleting_a_row_shows_up_as_a_gap(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        for _ in range(6):
            led.append(make_event())
    finally:
        led.close()

    conn = sqlite3.connect(str(ledger_path))
    try:
        conn.execute("DELETE FROM agent_events WHERE seq IN (3, 4)")
        conn.execute("DELETE FROM agent_events WHERE seq = 1")
        conn.commit()
    finally:
        conn.close()

    led = Ledger(ledger_path, DEVICE)
    try:
        assert led.missing_ranges() == [(1, 1), (3, 4)]
    finally:
        led.close()


def test_reopen_resumes_the_same_chain(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        for _ in range(3):
            led.append(make_event())
        head_before = led.head
        next_before = led.next_seq
    finally:
        led.close()  # simulates a clean crash boundary

    reopened = Ledger(ledger_path, DEVICE)
    try:
        assert reopened.head == head_before
        assert reopened.next_seq == next_before == 4
        entry = reopened.append(make_event())
        assert entry.seq == 4
        assert entry.prev_hash == head_before
        assert reopened.missing_ranges() == []
    finally:
        reopened.close()

    assert first_broken_seq(ledger_path) is None


def test_reopen_after_hard_kill_keeps_chain_verifiable(ledger_path):
    """A process that dies without close() must not orphan the chain."""
    led = Ledger(ledger_path, DEVICE)
    for _ in range(4):
        led.append(make_event())
    head = led.head
    del led  # no close(): WAL contents must still be readable by the next open

    reopened = Ledger(ledger_path, DEVICE)
    try:
        assert reopened.head == head
        reopened.append(make_event())
        assert first_broken_seq(ledger_path) is None
    finally:
        reopened.close()


def test_concurrent_appends_keep_seq_contiguous_and_chain_valid(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    threads = 8
    per_thread = 25
    seqs: list[int] = []
    seqs_lock = threading.Lock()
    errors: list[BaseException] = []
    start = threading.Barrier(threads)

    def worker(n: int) -> None:
        try:
            start.wait(timeout=10)
            local = []
            for i in range(per_thread):
                entry = led.append(make_event(session_id=f"sess_{n}", payload={"i": i, "t": n}))
                local.append(entry.seq)
            with seqs_lock:
                seqs.extend(local)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    try:
        workers = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=60)
        assert not errors, errors
        assert sorted(seqs) == list(range(1, threads * per_thread + 1))
        assert led.next_seq == threads * per_thread + 1
        assert led.missing_ranges() == []
    finally:
        led.close()

    assert first_broken_seq(ledger_path) is None


class _FailingConn:
    """Wraps a live connection and fails writes, like a full disk would."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.fail = True

    def execute(self, sql: str, *args):
        if self.fail and sql.lstrip().upper().startswith(("BEGIN", "INSERT")):
            raise sqlite3.OperationalError("database or disk is full")
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_strict_mode_raises_on_write_failure(ledger_path):
    led = Ledger(ledger_path, DEVICE, strict_mode=True)
    try:
        led.append(make_event())
        broken = _FailingConn(led._conn)
        led._conn = broken

        with pytest.raises(LedgerWriteError):
            led.append(make_event())

        # Chain state must not have advanced past the entry that was persisted.
        broken.fail = False
        assert led.next_seq == 2
        entry = led.append(make_event())
        assert entry.seq == 2
        led._conn = broken._real
    finally:
        led.close()

    assert first_broken_seq(ledger_path) is None


def test_non_strict_mode_records_a_record_failure_marker(ledger_path):
    led = Ledger(ledger_path, DEVICE, strict_mode=False)
    try:
        led.append(make_event())
        broken = _FailingConn(led._conn)
        led._conn = broken

        dropped = make_event(session_id="sess_dropped", tool_name="Write")
        assert led.append(dropped) is None  # swallowed, never raised

        broken.fail = False
        survivor = led.append(make_event(session_id="sess_after"))
        led._conn = broken._real

        entries = list(led.iter_entries())
        kinds = [e.event.kind for e in entries]
        assert kinds == [
            EventKind.TOOL_USE.value,
            EventKind.RECORD_FAILURE.value,
            EventKind.TOOL_USE.value,
        ]

        marker = entries[1].event
        assert marker.device_id == DEVICE
        assert marker.payload["reason"] == "ledger_write_failed"
        assert marker.payload["dropped_count"] == 1
        assert marker.payload["dropped"][0]["event_id"] == dropped.event_id
        assert "disk is full" in marker.payload["dropped"][0]["error"]
        assert marker.payload_hash == sha256_hex(canonicalize(marker.payload))

        # The marker takes a seq of its own; the survivor comes after it.
        assert survivor.seq == 3
        assert led.missing_ranges() == []
    finally:
        led.close()

    assert first_broken_seq(ledger_path) is None


def test_non_strict_marker_is_written_once_for_multiple_failures(ledger_path):
    led = Ledger(ledger_path, DEVICE, strict_mode=False)
    try:
        broken = _FailingConn(led._conn)
        led._conn = broken
        for _ in range(3):
            assert led.append(make_event()) is None
        broken.fail = False
        led.append(make_event())
        led._conn = broken._real

        markers = [
            e for e in led.iter_entries() if e.event.kind == EventKind.RECORD_FAILURE.value
        ]
        assert len(markers) == 1
        assert markers[0].event.payload["dropped_count"] == 3
        assert markers[0].seq == 1
    finally:
        led.close()


def test_checkpoints_roundtrip(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        for _ in range(3):
            led.append(make_event())
        assert led.latest_checkpoint() is None
        cp = {
            "device_id": DEVICE,
            "seq_start": 1,
            "seq_end": 3,
            "chain_head": led.head,
            "ts_device": "2026-08-10T12:00:01.000000Z",
            "sig": "ed25519:AAAA",
        }
        led.append_checkpoint(cp)
        assert led.latest_checkpoint() == cp

        for _ in range(2):
            led.append(make_event())
        cp2 = dict(cp, seq_start=4, seq_end=5, chain_head=led.head, sig="ed25519:BBBB")
        led.append_checkpoint(cp2)
        assert led.latest_checkpoint() == cp2
        assert [c["seq_end"] for c in led.iter_checkpoints()] == [3, 5]
    finally:
        led.close()


def test_checkpoint_sync_watermark_roundtrip(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        assert led.get_synced_checkpoint_up_to() == 0
        assert led.read_unsynced_checkpoints() == []

        for _ in range(3):
            led.append(make_event())
        cp1 = {
            "device_id": DEVICE,
            "seq_start": 1,
            "seq_end": 3,
            "chain_head": led.head,
            "ts_device": "2026-08-10T12:00:01.000000Z",
            "sig": "ed25519:AAAA",
        }
        led.append_checkpoint(cp1)

        for _ in range(2):
            led.append(make_event())
        cp2 = dict(cp1, seq_start=4, seq_end=5, chain_head=led.head, sig="ed25519:BBBB")
        led.append_checkpoint(cp2)

        assert led.read_unsynced_checkpoints() == [cp1, cp2]
        assert led.read_unsynced_checkpoints(limit=1) == [cp1]

        led.set_synced_checkpoint_up_to(3)
        assert led.get_synced_checkpoint_up_to() == 3
        assert led.read_unsynced_checkpoints() == [cp2]

        led.set_synced_checkpoint_up_to(5)
        assert led.read_unsynced_checkpoints() == []
    finally:
        led.close()


def test_checkpoint_sync_watermark_refuses_to_move_backwards(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        led.append(make_event())
        cp = {
            "device_id": DEVICE,
            "seq_start": 1,
            "seq_end": 1,
            "chain_head": led.head,
            "ts_device": "2026-08-10T12:00:01.000000Z",
            "sig": "ed25519:AAAA",
        }
        led.append_checkpoint(cp)
        led.set_synced_checkpoint_up_to(1)
        with pytest.raises(ValueError):
            led.set_synced_checkpoint_up_to(0)
    finally:
        led.close()


def test_checkpoint_sync_watermark_refuses_to_move_past_latest_checkpoint(ledger_path):
    led = Ledger(ledger_path, DEVICE)
    try:
        led.append(make_event())
        cp = {
            "device_id": DEVICE,
            "seq_start": 1,
            "seq_end": 1,
            "chain_head": led.head,
            "ts_device": "2026-08-10T12:00:01.000000Z",
            "sig": "ed25519:AAAA",
        }
        led.append_checkpoint(cp)
        with pytest.raises(ValueError):
            led.set_synced_checkpoint_up_to(2)
    finally:
        led.close()


def test_checkpoint_sync_watermark_independent_of_entry_watermark(ledger_path):
    # Shipping checkpoints and shipping entries are two different batches
    # against two different endpoints; confirming one must never move the
    # other's watermark.
    led = Ledger(ledger_path, DEVICE)
    try:
        led.append(make_event())
        led.append(make_event())
        cp = {
            "device_id": DEVICE,
            "seq_start": 1,
            "seq_end": 2,
            "chain_head": led.head,
            "ts_device": "2026-08-10T12:00:01.000000Z",
            "sig": "ed25519:AAAA",
        }
        led.append_checkpoint(cp)

        led.set_synced_up_to(2)
        assert led.get_synced_checkpoint_up_to() == 0

        led.set_synced_checkpoint_up_to(2)
        assert led.get_synced_up_to() == 2
    finally:
        led.close()


def test_close_is_idempotent_and_blocks_further_writes(ledger_path):
    led = Ledger(ledger_path, DEVICE, strict_mode=True)
    led.append(make_event())
    led.close()
    led.close()
    with pytest.raises(LedgerWriteError):
        led.append(make_event())


def test_append_after_close_is_swallowed_in_non_strict_mode(ledger_path):
    # A closed ledger is a write failure like any other: non-strict mode
    # must never let it crash the caller, same as any other append failure.
    led = Ledger(ledger_path, DEVICE, strict_mode=False)
    led.append(make_event())
    led.close()

    assert led.append(make_event()) is None


def test_appends_after_close_do_not_grow_pending_failures_forever(ledger_path):
    # A closed ledger can never append again, so it can never drain a queued
    # record_failure marker either — queuing one per dropped append would
    # leak memory for the lifetime of a long-lived process that keeps a
    # stale Ledger reference around after close().
    led = Ledger(ledger_path, DEVICE, strict_mode=False)
    led.append(make_event())
    led.close()

    for _ in range(5):
        assert led.append(make_event()) is None

    assert led._pending_failures == []


def test_seq_is_bound_into_the_digest(ledger_path):
    """Replaying a row at a different seq must not verify."""
    led = Ledger(ledger_path, DEVICE)
    try:
        for _ in range(3):
            led.append(make_event())
    finally:
        led.close()

    conn = sqlite3.connect(str(ledger_path))
    try:
        # Move seq 3's event body onto seq 2's row, hashes and all.
        row = conn.execute(
            "SELECT payload, payload_hash, event_id, event_digest FROM agent_events WHERE seq = 3"
        ).fetchone()
        conn.execute("DELETE FROM agent_events WHERE seq = 3")
        conn.execute(
            "UPDATE agent_events SET payload = ?, payload_hash = ?, event_id = ?, "
            "event_digest = ? WHERE seq = 2",
            row,
        )
        conn.commit()
    finally:
        conn.close()

    assert first_broken_seq(ledger_path) == 2

"""
ledger.py — the per-device, hash-chained, append-only event ledger.

One SQLite file per device. Every appended event gets a monotonic ``seq`` and
an ``entry_hash`` that commits to the previous entry's hash, so any later edit
to a persisted row — payload, timestamp, tool name, anything — breaks the chain
at exactly the row that was touched and at every row after it. That is the
whole point: the ledger is not trusted storage, it is *evidence* whose
tampering is detectable offline by a third party.

Design notes:

- Chain per device, never one global chain (spec §6.1/§6.2). Writes serialize
  within a single Ledger instance only, so N devices scale linearly.
- WAL + ``synchronous=NORMAL``: readers (the verifier, checkpointer) never
  block on an in-flight append, and an append costs a single fsync-free write.
  NORMAL can lose the last transactions on an OS-level crash — that shows up
  as a *gap*, which ``missing_ranges()`` reports honestly, rather than as a
  silently rewritten history.
- One transaction per append. Batching would be faster and would also mean an
  unrecorded action could be lost after the proxy already let it through.
- Thread-safe: the proxy calls this from async handlers (usually via
  ``asyncio.to_thread``), so a single connection opened with
  ``check_same_thread=False`` is guarded by one lock that covers
  read-head → hash → write as one critical section.
- Resume from disk on open: after a crash the chain continues from the last
  persisted ``entry_hash``; it never restarts at genesis.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.schema import (
    EVENT_SCHEMA_VERSION,
    AgentEvent,
    EventKind,
    event_digest,
    new_event_id,
    now_monotonic_ns,
    now_ts_device,
)

log = logging.getLogger(__name__)

# The chain's fixed starting point. An empty ledger's head is genesis, so a
# verifier can tell "nothing was ever recorded" apart from "the first entries
# were deleted" (the latter leaves seq 1's prev_hash != genesis or a gap).
GENESIS_PREV_HASH = "sha256:" + "00" * 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_events (
    seq             INTEGER PRIMARY KEY,
    event_id        TEXT NOT NULL UNIQUE,
    schema_version  TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    ts_device       TEXT NOT NULL,
    ts_monotonic_ns INTEGER NOT NULL,
    tool_use_id     TEXT,
    tool_name       TEXT,
    payload         TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    model           TEXT,
    provider        TEXT NOT NULL,
    event_digest    TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    entry_hash      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events_session ON agent_events(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_tool_use ON agent_events(tool_use_id);

CREATE TABLE IF NOT EXISTS checkpoints (
    seq_end     INTEGER PRIMARY KEY,
    device_id   TEXT NOT NULL,
    seq_start   INTEGER NOT NULL,
    chain_head  TEXT NOT NULL,
    ts_device   TEXT NOT NULL,
    sig         TEXT NOT NULL,
    body        TEXT NOT NULL
);

-- Single-row table: how far a shipper has confirmed delivery to Coriqo.
-- Lives in the ledger itself (not shipper-process memory) so "what's been
-- synced" survives a proxy restart exactly like the chain it describes.
CREATE TABLE IF NOT EXISTS sync_state (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    synced_up_to_seq INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
);
"""


class LedgerWriteError(RuntimeError):
    """A ledger write failed while ``strict_mode`` was on.

    The caller (the proxy) turns this into a 503: in a regulated deployment an
    action the ledger could not record must not be allowed to happen.
    """


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One persisted row: the event plus its position in the chain."""

    seq: int
    prev_hash: str
    entry_hash: str
    event_digest: str
    event: AgentEvent


def compute_entry_hash(prev_hash: str, seq: int, digest: str) -> str:
    """The chain link: sha256 over the canonical {prev_hash, seq, event_digest}.

    Exposed at module level so the offline verifier recomputes links with the
    exact same function the writer used, instead of a re-implementation that
    can drift.
    """
    return sha256_hex(
        canonicalize({"prev_hash": prev_hash, "seq": seq, "event_digest": digest})
    )


class Ledger:
    """Append-only hash-chained event log for a single device."""

    def __init__(
        self, path: str | Path, device_id: str, *, strict_mode: bool = False
    ) -> None:
        self.path = Path(path)
        self.device_id = device_id
        self.strict_mode = strict_mode

        # One lock for everything: append's read-head/hash/insert must be
        # atomic against other appends, and sqlite3 objects are not safe to
        # use concurrently from multiple threads even with check_same_thread
        # disabled.
        self._lock = threading.RLock()
        self._closed = False
        # Write failures swallowed in non-strict mode, drained into a
        # record_failure marker on the next successful append.
        self._pending_failures: list[dict[str, Any]] = []

        os.makedirs(self.path.parent or Path("."), exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

        self._head, self._next_seq = self._resume()

    # ---------------------------------------------------------------- state

    def _resume(self) -> tuple[str, int]:
        """Restore the chain head from disk so a crash resumes, not restarts."""
        row = self._conn.execute(
            "SELECT seq, entry_hash FROM agent_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return GENESIS_PREV_HASH, 1
        return row[1], row[0] + 1

    @property
    def head(self) -> str:
        with self._lock:
            return self._head

    @property
    def next_seq(self) -> int:
        with self._lock:
            return self._next_seq

    # --------------------------------------------------------------- append

    def append(self, event: AgentEvent) -> LedgerEntry | None:
        """Assign a seq, link it into the chain, and commit it in one transaction.

        Returns the persisted entry. Returns ``None`` only in the non-strict
        failure path, where the write is dropped, logged loudly, and remembered
        so the next successful append carries a ``record_failure`` marker
        (contract deviation: the contract types this as ``LedgerEntry``, but
        there is no honest entry to return when nothing was persisted).
        """
        with self._lock:
            self._require_open()
            if self._pending_failures:
                self._drain_failures()
            try:
                return self._append_locked(event)
            except Exception as exc:  # noqa: BLE001 - policy decision below
                self._on_write_failure(event, exc)
                return None

    def _append_locked(self, event: AgentEvent) -> LedgerEntry:
        seq = self._next_seq
        prev_hash = self._head
        # The event carries the seq that is actually written, so the digest
        # commits to its position too — a row cannot be reordered or replayed
        # at a different seq without breaking its own digest.
        stamped = _with_seq(event, seq)
        digest = event_digest(stamped)
        entry_hash = compute_entry_hash(prev_hash, seq, digest)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO agent_events (seq, event_id, schema_version, device_id, "
                "session_id, kind, ts_device, ts_monotonic_ns, tool_use_id, tool_name, "
                "payload, payload_hash, model, provider, event_digest, prev_hash, "
                "entry_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    stamped.event_id,
                    stamped.schema_version,
                    stamped.device_id,
                    stamped.session_id,
                    _kind_value(stamped.kind),
                    stamped.ts_device,
                    stamped.ts_monotonic_ns,
                    stamped.tool_use_id,
                    stamped.tool_name,
                    canonicalize(stamped.payload).decode("utf-8"),
                    stamped.payload_hash,
                    stamped.model,
                    stamped.provider,
                    digest,
                    prev_hash,
                    entry_hash,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

        # Only advance in-memory chain state after the row is durable, so a
        # failed write cannot leave the head pointing at an entry nobody has.
        self._head = entry_hash
        self._next_seq = seq + 1
        return LedgerEntry(
            seq=seq,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            event_digest=digest,
            event=stamped,
        )

    def _on_write_failure(self, event: AgentEvent, exc: Exception) -> None:
        log.error(
            "ledger write failed for event %s (kind=%s, session=%s): %s",
            getattr(event, "event_id", "?"),
            _kind_value(getattr(event, "kind", "?")),
            getattr(event, "session_id", "?"),
            exc,
        )
        if self.strict_mode:
            raise LedgerWriteError(
                f"failed to record event {getattr(event, 'event_id', '?')}: {exc}"
            ) from exc
        self._pending_failures.append(
            {
                "event_id": getattr(event, "event_id", None),
                "kind": _kind_value(getattr(event, "kind", None)),
                "session_id": getattr(event, "session_id", None),
                "tool_use_id": getattr(event, "tool_use_id", None),
                "error": f"{type(exc).__name__}: {exc}",
                "ts_device": now_ts_device(),
            }
        )

    def _drain_failures(self) -> None:
        """Write one record_failure marker covering everything we dropped.

        Best-effort by construction: if the marker itself cannot be written the
        failures stay pending and we try again on the next append, rather than
        recursing or losing the fact that something went unrecorded.
        """
        failures = list(self._pending_failures)
        payload: dict[str, Any] = {
            "reason": "ledger_write_failed",
            "dropped_count": len(failures),
            "dropped": failures,
        }
        marker = AgentEvent(
            schema_version=EVENT_SCHEMA_VERSION,
            event_id=new_event_id(),
            device_id=self.device_id,
            session_id=failures[-1].get("session_id") or "unknown",
            seq=0,
            kind=_kind_value(EventKind.RECORD_FAILURE),
            ts_device=now_ts_device(),
            ts_monotonic_ns=now_monotonic_ns(),
            tool_use_id=None,
            tool_name=None,
            payload=payload,
            payload_hash=sha256_hex(canonicalize(payload)),
            model=None,
            provider="anthropic",
        )
        try:
            self._append_locked(marker)
        except Exception as exc:  # noqa: BLE001 - never raise from the marker path
            log.error("could not write record_failure marker: %s", exc)
            return
        self._pending_failures.clear()

    # ----------------------------------------------------------------- read

    def read_range(self, start: int, end: int) -> list[LedgerEntry]:
        """Entries with ``start <= seq <= end`` (both inclusive), in seq order."""
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT * FROM agent_events WHERE seq >= ? AND seq <= ? ORDER BY seq",
                (start, end),
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def iter_entries(self) -> Iterator[LedgerEntry]:
        """Stream the whole chain in seq order without loading it all at once."""
        with self._lock:
            self._require_open()
            cur = self._conn.execute("SELECT * FROM agent_events ORDER BY seq")
            try:
                while True:
                    rows = cur.fetchmany(256)
                    if not rows:
                        return
                    for row in rows:
                        yield _row_to_entry(row)
            finally:
                cur.close()

    def missing_ranges(self) -> list[tuple[int, int]]:
        """Inclusive (start, end) seq ranges that should exist but do not.

        A gap means events were lost (crash before commit) or deleted. Either
        way it is a finding, not something to paper over.
        """
        with self._lock:
            self._require_open()
            seqs = [r[0] for r in self._conn.execute("SELECT seq FROM agent_events ORDER BY seq")]
        if not seqs:
            return []
        gaps: list[tuple[int, int]] = []
        # The chain is defined to start at seq 1, so a missing prefix is a gap.
        if seqs[0] > 1:
            gaps.append((1, seqs[0] - 1))
        for prev, cur in zip(seqs, seqs[1:], strict=False):
            if cur > prev + 1:
                gaps.append((prev + 1, cur - 1))
        return gaps

    # ------------------------------------------------------------- sync

    def read_unsynced(self, limit: int | None = None) -> list[LedgerEntry]:
        """Entries after the sync watermark, in seq order.

        This is what a shipper batches and sends. It does not move the
        watermark — call :meth:`set_synced_up_to` only after the server has
        confirmed the batch, so a crash mid-ship just means the next attempt
        resends a few already-accepted rows (safe: the server dedupes on
        ``entry_hash``).
        """
        with self._lock:
            self._require_open()
            sql = "SELECT * FROM agent_events WHERE seq > ? ORDER BY seq"
            params: tuple[Any, ...] = (self._get_synced_up_to_locked(),)
            if limit is not None:
                sql += " LIMIT ?"
                params = (*params, limit)
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_entry(r) for r in rows]

    def get_synced_up_to(self) -> int:
        """The highest seq the shipper has confirmed Coriqo received. 0 if never shipped."""
        with self._lock:
            self._require_open()
            return self._get_synced_up_to_locked()

    def _get_synced_up_to_locked(self) -> int:
        row = self._conn.execute(
            "SELECT synced_up_to_seq FROM sync_state WHERE id = 1"
        ).fetchone()
        return row[0] if row is not None else 0

    def set_synced_up_to(self, seq: int) -> None:
        """Advance the watermark. Refuses to move it backwards or past the chain head.

        Both would silently hide unsynced data: moving it backwards would
        make already-confirmed rows look unsynced again (harmless but
        wasteful); moving it past the head would make rows that were never
        written look synced, which a real gap could exploit.
        """
        with self._lock:
            self._require_open()
            current = self._get_synced_up_to_locked()
            if seq < current:
                raise ValueError(
                    f"refusing to move sync watermark backwards: {seq} < {current}"
                )
            if seq > self._next_seq - 1:
                raise ValueError(
                    f"refusing to mark seq {seq} synced: chain head is at {self._next_seq - 1}"
                )
            self._conn.execute(
                "INSERT INTO sync_state (id, synced_up_to_seq, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET synced_up_to_seq = excluded.synced_up_to_seq, "
                "updated_at = excluded.updated_at",
                (seq, now_ts_device()),
            )

    # ---------------------------------------------------------- checkpoints

    def append_checkpoint(self, cp: dict) -> None:
        """Persist a signed checkpoint (written by the checkpointer, §6.2)."""
        with self._lock:
            self._require_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO checkpoints (seq_end, device_id, seq_start, "
                    "chain_head, ts_device, sig, body) VALUES (?,?,?,?,?,?,?)",
                    (
                        int(cp["seq_end"]),
                        cp["device_id"],
                        int(cp["seq_start"]),
                        cp["chain_head"],
                        cp["ts_device"],
                        cp["sig"],
                        canonicalize(cp).decode("utf-8"),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def latest_checkpoint(self) -> dict | None:
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT body FROM checkpoints ORDER BY seq_end DESC LIMIT 1"
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def iter_checkpoints(self) -> list[dict]:
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT body FROM checkpoints ORDER BY seq_end"
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---------------------------------------------------------------- close

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                log.warning("error closing ledger %s: %s", self.path, exc)

    def _require_open(self) -> None:
        if self._closed:
            raise LedgerWriteError(f"ledger {self.path} is closed")

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _kind_value(kind: Any) -> Any:
    """Accept either an EventKind or the bare string the dataclass declares."""
    return kind.value if isinstance(kind, EventKind) else kind


def _with_seq(event: AgentEvent, seq: int) -> AgentEvent:
    """Return the event stamped with its assigned seq (AgentEvent is frozen)."""
    if getattr(event, "seq", None) == seq:
        return event
    fields = {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "device_id": event.device_id,
        "session_id": event.session_id,
        "seq": seq,
        "kind": _kind_value(event.kind),
        "ts_device": event.ts_device,
        "ts_monotonic_ns": event.ts_monotonic_ns,
        "tool_use_id": event.tool_use_id,
        "tool_name": event.tool_name,
        "payload": event.payload,
        "payload_hash": event.payload_hash,
        "model": event.model,
        "provider": event.provider,
    }
    return AgentEvent(**fields)


def _row_to_entry(row: tuple) -> LedgerEntry:
    (
        seq,
        event_id,
        schema_version,
        device_id,
        session_id,
        kind,
        ts_device,
        ts_monotonic_ns,
        tool_use_id,
        tool_name,
        payload,
        payload_hash,
        model,
        provider,
        digest,
        prev_hash,
        entry_hash,
    ) = row
    event = AgentEvent(
        schema_version=schema_version,
        event_id=event_id,
        device_id=device_id,
        session_id=session_id,
        seq=seq,
        kind=kind,
        ts_device=ts_device,
        ts_monotonic_ns=ts_monotonic_ns,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        payload=json.loads(payload),
        payload_hash=payload_hash,
        model=model,
        provider=provider,
    )
    return LedgerEntry(
        seq=seq,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        event_digest=digest,
        event=event,
    )

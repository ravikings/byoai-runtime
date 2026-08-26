"""SQLite-backed ingest read model.

Design constraints, all forced by what ``recorder/shipper.py`` actually sends
(verified against the wire, not the contract docs, which disagree with it in
several places):

**The batch carries no tenant.** Not in the body, not in a header — only
``x-coriqo-device`` and a signature. Tenancy is therefore *derived from the
enrolment record that bound the device*, never read off the request. A store
that trusted a client-supplied tenant would let any device write into any
tenant's evidence, which for this product is the whole ballgame.

**Delivery is at-least-once.** The shipper advances its watermark only after a
2xx, so a crash between the server commit and that write resends the batch.
Dedupe is a correctness requirement, not an optimisation: ``entry_hash`` for
entries, ``(device_id, seq_end)`` for checkpoints.

**``device_id`` is not stable.** It is ``dev_`` + base32(sha256(public key)),
so rotating a key produces a *new* device. A physical machine is a chain of
device_ids stitched by a ``key_rotated`` event that appears in the OLD
device's stream. ``enrolments.supersedes_device_id`` records that link so
"40 devices" can mean forty machines rather than forty keys.

**``prev_hash`` and ``event_digest`` are not shipped.** Only ``entry_hash``.
The chain cannot be re-linked here from batches alone; verification remains a
device-side, offline act. This store deliberately does not claim otherwise —
it records what arrived and what is missing, and leaves proof to
``coriqo-verify``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

-- Enrolment is the only reason this store can report a device that never
-- shipped. Without it, silence and non-existence are the same row count.
CREATE TABLE IF NOT EXISTS enrolments (
    device_id             TEXT PRIMARY KEY,
    tenant_id             INTEGER NOT NULL REFERENCES tenants(tenant_id),
    public_key_b64        TEXT NOT NULL,
    enrolled_at           TEXT NOT NULL,
    revoked_at            TEXT,
    -- Set when this identity replaced another via key rotation, so a machine
    -- can be followed across the device_ids it has worn.
    supersedes_device_id  TEXT REFERENCES enrolments(device_id),
    -- Observed or declared ship cadence, seconds. NULL means not enough
    -- batches to infer one — and an unknown cadence must never be rendered as
    -- an on-time one, so it stays NULL rather than defaulting.
    expected_interval_s   REAL
);
CREATE INDEX IF NOT EXISTS idx_enrolments_tenant ON enrolments(tenant_id);

CREATE TABLE IF NOT EXISTS entries (
    device_id    TEXT NOT NULL REFERENCES enrolments(device_id),
    seq          INTEGER NOT NULL,
    entry_hash   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    session_id   TEXT,
    ts_device    TEXT,
    trace_id     TEXT,
    event_json   TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, seq)
);
-- Dedupe key for at-least-once redelivery, scoped PER DEVICE.
--
-- A global unique index here was a cross-tenant evidence-suppression hole:
-- entry_hash is attacker-chosen wire data, so a hostile device could insert a
-- hash its victim was about to ship, and the victim's genuine entry would be
-- silently dropped as a "duplicate" — reported back to the shipper as success,
-- letting its watermark advance past evidence that was never stored. Scoping
-- the constraint to the device makes squatting impossible; a hash that recurs
-- across devices is surfaced as an integrity finding instead of swallowed.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_device_hash ON entries(device_id, entry_hash);
CREATE INDEX IF NOT EXISTS idx_entries_hash ON entries(entry_hash);
CREATE INDEX IF NOT EXISTS idx_entries_device_kind ON entries(device_id, kind);
CREATE INDEX IF NOT EXISTS idx_entries_session ON entries(device_id, session_id);
CREATE INDEX IF NOT EXISTS idx_entries_received ON entries(received_at);

CREATE TABLE IF NOT EXISTS checkpoints (
    device_id    TEXT NOT NULL REFERENCES enrolments(device_id),
    seq_start    INTEGER NOT NULL,
    seq_end      INTEGER NOT NULL,
    chain_head   TEXT NOT NULL,
    ts_device    TEXT NOT NULL,
    sig          TEXT NOT NULL,
    countersigned_at TEXT,
    received_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, seq_end)
);

-- Liveness is maintained on write rather than derived on read. A coverage
-- query that scanned every entry to find "when did this device last speak"
-- would be the slowest query in the product and it runs on the landing page.
CREATE TABLE IF NOT EXISTS device_state (
    device_id            TEXT PRIMARY KEY REFERENCES enrolments(device_id),
    first_batch_at       TEXT,
    -- Last time NEW evidence arrived. Deliberately not "last time we heard
    -- from the device": an empty or wholly-duplicate batch is contact, not
    -- evidence, and a device that replays old entries forever must not be able
    -- to keep itself out of the silence report for free.
    last_batch_at        TEXT,
    -- Last contact of any kind, including duplicate-only batches. Useful for
    -- diagnosing a device that is alive but producing nothing; never used as
    -- the liveness signal in coverage().
    last_contact_at      TEXT,
    last_seq_received    INTEGER,
    entries_received     INTEGER NOT NULL DEFAULT 0,
    batches_received     INTEGER NOT NULL DEFAULT 0,
    last_checkpoint_at   TEXT,
    last_checkpoint_seq  INTEGER,
    -- Gaps are maintained on write, like the other observations here. Deriving
    -- them per call meant a full scan-and-sort of a device's entire history on
    -- every batch AND twice per device on coverage() — which is the landing
    -- page, so the most-run query in the product grew with history size.
    gaps_json            TEXT NOT NULL DEFAULT '[]'
);
"""


def _require_seq(value: object) -> int:
    """A seq is a non-negative int, and nothing else.

    Left unvalidated, ``bool`` coerced to 0/1, numeric strings coerced
    silently, and a negative next to a huge value made ``seq_gaps`` emit one
    "gap" spanning the integer line — a one-batch denial of service against the
    coverage report.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedEntry(f"seq must be an int, got {value!r}")
    if value < 0:
        raise MalformedEntry(f"seq must be non-negative, got {value}")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Enrolment:
    device_id: str
    tenant_slug: str
    public_key_b64: str
    enrolled_at: str
    supersedes_device_id: str | None = None
    expected_interval_s: float | None = None


@dataclass(frozen=True, slots=True)
class AcceptResult:
    """Mirrors the ingest response shape the shipper already parses."""

    accepted: int
    duplicates: int
    gaps: list[list[int]]


class IngestStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> IngestStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- write

    def ensure_tenant(self, slug: str) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO tenants (slug, created_at) VALUES (?, ?)", (slug, _now())
            )
            row = self._conn.execute(
                "SELECT tenant_id FROM tenants WHERE slug = ?", (slug,)
            ).fetchone()
            assert row is not None
            return int(row["tenant_id"])

    def record_enrolment(self, e: Enrolment) -> None:
        """Bind a device to a tenant. This is the ONLY place tenancy is set.

        Three refusals, each closing a way to write into another tenant:

        * A ``device_id`` must be the one derived from its public key. Without
          that binding an arbitrary id can be enrolled against an unrelated
          key, which is the primitive behind the next refusal.
        * Re-enrolling an existing ``device_id`` under a different tenant is an
          error, not a silent no-op. Previously the tenant was (correctly) not
          updated but the ``public_key_b64`` WAS — so anyone with a token for
          their own tenant could re-point a victim's device at their own key
          and have their signed batches admitted as the victim's evidence.
        * ``supersedes_device_id`` must name a device in the same tenant, or a
          rotation lineage can be fabricated across a tenant boundary.
        """
        from byoai.recorder.keys import derive_device_id

        expected = derive_device_id(e.public_key_b64)
        if expected != e.device_id:
            raise EnrolmentRefused(
                f"device_id {e.device_id!r} is not derived from the supplied public key "
                f"(expected {expected!r})"
            )

        tenant_id = self.ensure_tenant(e.tenant_slug)
        with self._lock:
            prior = self._conn.execute(
                "SELECT tenant_id, public_key_b64 FROM enrolments WHERE device_id = ?",
                (e.device_id,),
            ).fetchone()
            if prior is not None and int(prior["tenant_id"]) != tenant_id:
                raise EnrolmentRefused(
                    f"device {e.device_id!r} is already enrolled in another tenant; "
                    "re-enrolment across tenants is refused"
                )
            if e.supersedes_device_id is not None:
                pred = self._conn.execute(
                    "SELECT tenant_id FROM enrolments WHERE device_id = ?",
                    (e.supersedes_device_id,),
                ).fetchone()
                if pred is None or int(pred["tenant_id"]) != tenant_id:
                    raise EnrolmentRefused(
                        f"supersedes_device_id {e.supersedes_device_id!r} is not a device "
                        "in this tenant"
                    )
            self._conn.execute(
                """INSERT INTO enrolments
                     (device_id, tenant_id, public_key_b64, enrolled_at,
                      supersedes_device_id, expected_interval_s)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     public_key_b64 = excluded.public_key_b64,
                     supersedes_device_id = COALESCE(excluded.supersedes_device_id,
                                                     enrolments.supersedes_device_id),
                     expected_interval_s = COALESCE(excluded.expected_interval_s,
                                                    enrolments.expected_interval_s)""",
                (
                    e.device_id,
                    tenant_id,
                    e.public_key_b64,
                    e.enrolled_at,
                    e.supersedes_device_id,
                    e.expected_interval_s,
                ),
            )
            # A device_state row exists from enrolment, with every observation
            # column NULL. That NULL is the "never seen" signal the coverage
            # report is built on — an absent row would be indistinguishable
            # from an unenrolled machine.
            self._conn.execute(
                "INSERT OR IGNORE INTO device_state (device_id) VALUES (?)", (e.device_id,)
            )

    def accept_batch(self, device_id: str, entries: Iterable[dict[str, Any]]) -> AcceptResult:
        """Persist an accepted event batch.

        Assumes the caller has already authenticated the device and verified
        the batch signature — this class stores evidence, it does not decide
        whether to trust it. `device_id` comes from the authenticated header,
        never from the body.
        """
        accepted = 0
        duplicates = 0
        batch_max_seq: int | None = None
        accepted_seqs: list[int] = []
        with self._lock:
            self._require_writable(device_id)
            head_row = self._conn.execute(
                "SELECT last_seq_received FROM device_state WHERE device_id = ?", (device_id,)
            ).fetchone()
            prev_head = None if head_row is None else head_row["last_seq_received"]
            # One transaction for the whole batch. Autocommitting per row meant
            # a malformed entry left earlier rows committed with no result
            # returned and liveness never updated — a half-ingested batch that
            # neither side knows about.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for entry in entries:
                    seq = _require_seq(entry.get("seq"))
                    entry_hash = entry.get("entry_hash")
                    if not isinstance(entry_hash, str) or entry_hash == "":
                        raise MalformedEntry(
                            f"entry_hash must be a non-empty string, got {entry_hash!r}"
                        )
                    event = entry.get("event")
                    if not isinstance(event, dict):
                        raise MalformedEntry(f"event must be an object, got {type(event).__name__}")
                    # The event's own device_id is untrusted wire data; the
                    # authenticated header is the truth. Disagreement is a
                    # finding, not something to normalise away silently.
                    claimed = event.get("device_id")
                    if claimed is not None and claimed != device_id:
                        raise MalformedEntry(
                            f"event.device_id {claimed!r} disagrees with the authenticated "
                            f"device {device_id!r}"
                        )
                    prior = self._conn.execute(
                        "SELECT entry_hash FROM entries WHERE device_id = ? AND seq = ?",
                        (device_id, seq),
                    ).fetchone()
                    if prior is not None and prior["entry_hash"] != entry_hash:
                        # Redelivery is byte-identical by definition. The same
                        # seq carrying DIFFERENT content is a contradiction —
                        # a bug or a tamper — and swallowing it as a duplicate
                        # kept the first body, reported success, and left no
                        # record that a conflicting one was ever offered. The
                        # store already refuses cross-device hash collisions;
                        # refusing this is the same principle one scope in.
                        raise SeqConflict(device_id, seq, str(prior["entry_hash"]), entry_hash)
                    other = self._conn.execute(
                        "SELECT device_id FROM entries WHERE entry_hash = ? AND device_id != ?",
                        (entry_hash, device_id),
                    ).fetchone()
                    if other is not None:
                        # Abort the whole batch, do not store and do not count.
                        # Detecting the squat but committing it anyway left the
                        # attacker's row in place and moved their device out of
                        # never_seen — closing the reporting hole while leaving
                        # the write itself, which is barely better than nothing.
                        raise EntryHashCollision(device_id, [entry_hash])
                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO entries
                             (device_id, seq, entry_hash, kind, session_id, ts_device,
                              trace_id, event_json, received_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            device_id,
                            seq,
                            entry_hash,
                            str(event.get("kind", "")),
                            event.get("session_id"),
                            event.get("ts_device"),
                            event.get("trace_id"),
                            json.dumps(event, separators=(",", ":"), sort_keys=True),
                            _now(),
                        ),
                    )
                    if cur.rowcount:
                        accepted += 1
                        accepted_seqs.append(seq)
                        batch_max_seq = seq if batch_max_seq is None else max(batch_max_seq, seq)
                    else:
                        duplicates += 1
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

            # True when every accepted seq extended the tail with no hole:
            # the common case, and the one where a full rescan proves nothing.
            expected = (0 if prev_head is None else int(prev_head) + 1)
            contiguous = sorted(accepted_seqs) == list(
                range(expected, expected + len(accepted_seqs))
            )
            self._touch_state(
                device_id,
                new_evidence=accepted > 0,
                accepted=accepted,
                max_seq=batch_max_seq,
                contiguous_tail=contiguous,
            )
            gaps = self.seq_gaps(device_id)
        return AcceptResult(accepted=accepted, duplicates=duplicates, gaps=gaps)

    def accept_checkpoints(
        self, device_id: str, checkpoints: Iterable[dict[str, Any]]
    ) -> AcceptResult:
        accepted = 0
        duplicates = 0
        with self._lock:
            # Same refusal as accept_batch. Leaning on the foreign key here
            # would surface an unenrolled device as a sqlite IntegrityError
            # rather than the typed error the caller branches on — and the two
            # ingest paths must agree about who is allowed to write, or the
            # coverage denominator can be moved through the quieter one.
            self._require_writable(device_id)

            # Atomic, for the same reason accept_batch is: a malformed
            # checkpoint partway through must not leave the earlier ones
            # committed with no result returned to the caller.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for cp in checkpoints:
                    seq_start = _require_seq(cp.get("seq_start"))
                    seq_end = _require_seq(cp.get("seq_end"))
                    if seq_end < seq_start:
                        raise MalformedEntry(
                            f"checkpoint seq_end {seq_end} precedes seq_start {seq_start}"
                        )
                    # Validated, not indexed. Plain dict access here let a
                    # checkpoint missing "sig" escape as a bare KeyError past
                    # the typed errors callers branch on — a malformed request
                    # surfacing as an unclassified failure instead of a refusal.
                    fields: dict[str, str] = {}
                    for name in ("chain_head", "ts_device", "sig"):
                        value = cp.get(name)
                        if not isinstance(value, str) or value == "":
                            raise MalformedEntry(
                                f"checkpoint {name} must be a non-empty string, got {value!r}"
                            )
                        fields[name] = value
                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO checkpoints
                             (device_id, seq_start, seq_end, chain_head, ts_device, sig,
                              received_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            device_id,
                            seq_start,
                            seq_end,
                            fields["chain_head"],
                            fields["ts_device"],
                            fields["sig"],
                            _now(),
                        ),
                    )
                    if cur.rowcount:
                        accepted += 1
                    else:
                        duplicates += 1
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
            # Contact, deliberately NOT evidence. A checkpoint asserts "I held
            # this much history", but nothing here validates seq_end against
            # entries actually received — so counting it as evidence would let
            # a device that has stopped recording forge its own liveness with a
            # signed claim about history it never shipped. Recording contact
            # fixes the real complaint (a checkpoint-only device was logged
            # nowhere at all) without buying that claim.
            self._touch_contact(device_id)
            self._conn.execute(
                """UPDATE device_state
                     SET last_checkpoint_at = ?,
                         last_checkpoint_seq = (SELECT MAX(seq_end) FROM checkpoints
                                                 WHERE device_id = ?)
                   WHERE device_id = ?""",
                (_now(), device_id, device_id),
            )
        return AcceptResult(accepted=accepted, duplicates=duplicates, gaps=[])

    def _require_writable(self, device_id: str) -> None:
        row = self._conn.execute(
            "SELECT revoked_at FROM enrolments WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise UnknownDeviceError(device_id)
        if row["revoked_at"] is not None:
            # A revoked key is a key we no longer believe. Continuing to accept
            # its evidence would let a device that was cut off keep writing
            # into the record it was cut off from.
            raise DeviceRevoked(device_id, str(row["revoked_at"]))

    def _touch_contact(self, device_id: str) -> None:
        """Record that the device spoke, whatever it had to say.

        Contact is not liveness — see ``_touch_state`` — but a device that only
        ever ships checkpoints was previously recorded nowhere at all, so it
        stayed in ``never_seen`` while visibly communicating. Both ingest paths
        call this; only one of them can also supply evidence.
        """
        self._conn.execute(
            "UPDATE device_state SET last_contact_at = ?, batches_received = batches_received + 1"
            " WHERE device_id = ?",
            (_now(), device_id),
        )

    def _touch_state(
        self,
        device_id: str,
        *,
        new_evidence: bool,
        accepted: int = 0,
        max_seq: int | None = None,
        contiguous_tail: bool = False,
    ) -> None:
        self._touch_contact(device_id)
        if not new_evidence:
            # Contact without evidence does not count as liveness. An empty or
            # wholly-duplicate batch used to move a device out of never_seen
            # and keep a dead device looking alive forever at zero cost —
            # defeating the one number this product exists to compute.
            return
        now = _now()
        self._conn.execute(
            """UPDATE device_state
                 SET first_batch_at = COALESCE(first_batch_at, ?),
                     last_batch_at = ?,
                     entries_received = entries_received + ?,
                     last_seq_received = MAX(COALESCE(last_seq_received, -1), COALESCE(?, -1))
               WHERE device_id = ?""",
            (now, now, accepted, max_seq, device_id),
        )
        # Recomputed only when entries actually landed, and only when the
        # batch could have changed the shape of the sequence. A device shipping
        # its next contiguous seqs — the steady state — leaves existing gaps
        # untouched and creates none, so the full history scan is skipped and
        # ingest cost stays proportional to the batch rather than to how long
        # the device has been running.
        if contiguous_tail:
            return
        self._conn.execute(
            "UPDATE device_state SET gaps_json = ? WHERE device_id = ?",
            (json.dumps(self._compute_seq_gaps(device_id)), device_id),
        )

    # ----------------------------------------------------------------- read

    def seq_gaps(self, device_id: str) -> list[list[int]]:
        """The device's known gaps, read from state maintained on write."""
        with self._lock:
            row = self._conn.execute(
                "SELECT gaps_json FROM device_state WHERE device_id = ?", (device_id,)
            ).fetchone()
        if row is None:
            return []
        parsed: list[list[int]] = json.loads(row["gaps_json"])
        return parsed

    def _compute_seq_gaps(self, device_id: str) -> list[list[int]]:
        """Holes in the received seq range for one device.

        Computed here rather than trusted from the device: the shipper's own
        ``missing_ranges()`` is client-side and never transmitted, and the
        existing mock only echoes test-injected gaps. Note the honest limit —
        this finds interior holes only. A device that stops at seq 500 looks
        identical to one that has exactly 500 events, which is why liveness,
        not gap detection, is what catches a silent device.
        """
        rows = self._conn.execute(
            "SELECT seq FROM entries WHERE device_id = ? ORDER BY seq", (device_id,)
        ).fetchall()
        out: list[list[int]] = []
        prev: int | None = None
        for row in rows:
            seq = int(row["seq"])
            if prev is not None and seq > prev + 1:
                out.append([prev + 1, seq - 1])
            prev = seq
        return out

    def devices(self, tenant_slug: str) -> list[dict[str, Any]]:
        # Same lock the writers hold. The connection is shared and opened with
        # check_same_thread=False, so a read racing an in-flight
        # BEGIN IMMEDIATE/COMMIT could observe entries counted before
        # device_state caught up — a coverage report straddling a commit.
        with self._lock:
            rows = self._conn.execute(
                """SELECT e.device_id, e.enrolled_at, e.revoked_at, e.supersedes_device_id,
                          e.expected_interval_s,
                          s.first_batch_at, s.last_batch_at, s.last_contact_at,
                          s.last_seq_received, s.entries_received, s.batches_received,
                          s.last_checkpoint_at, s.last_checkpoint_seq, s.gaps_json
                     FROM enrolments e
                     JOIN tenants t ON t.tenant_id = e.tenant_id
                     LEFT JOIN device_state s ON s.device_id = e.device_id
                    WHERE t.slug = ?
                    ORDER BY e.device_id""",
                (tenant_slug,),
            ).fetchall()
        return [dict(r) for r in rows]

    def coverage(self, tenant_slug: str) -> dict[str, Any]:
        """The silence report: what this tenant cannot account for.

        Every figure is computed against *enrolments*, never against the set of
        devices that happened to ship. The difference between those two
        denominators is the entire product.
        """
        devs = self.devices(tenant_slug)
        never_seen = [d for d in devs if d["last_batch_at"] is None]
        # A device can talk without producing evidence — checkpoints only, or
        # batches that were entirely redelivery. Folding that into never_seen
        # would call an actively-communicating device unheard-of; folding it
        # into reporting would credit it with evidence it never shipped. It is
        # a third state and gets its own list.
        talking_no_evidence = [
            d for d in never_seen if d["last_contact_at"] is not None
        ]
        seen = [d for d in devs if d["last_batch_at"] is not None]
        no_checkpoint = [d for d in seen if d["last_checkpoint_seq"] is None]
        # gaps_json rides along on the devices() join; querying it again per
        # device made the landing page's query count grow with fleet size for
        # a column already in hand.
        gaps: dict[str, list[list[int]]] = {}
        for d in seen:
            g = json.loads(d.get("gaps_json") or "[]")
            if g:
                gaps[d["device_id"]] = g
        return {
            "tenant": tenant_slug,
            "as_of": _now(),
            "enrolled": len(devs),
            "never_seen": never_seen,
            "contact_without_evidence": talking_no_evidence,
            "reporting": seen,
            "devices_without_checkpoint": no_checkpoint,
            "seq_gaps": gaps,
            # The product naming the limit of its own claim. A machine that
            # never enrolled produces no device_id, ships no batch and raises
            # no finding: it is absent from every number here, including
            # `enrolled`.
            "blind_spot": {
                "basis": "enrolments",
                "statement": (
                    "This report is computed against enrolment records. A host that never "
                    "enrolled is absent from every count here, including the denominator."
                ),
            },
        }


class EnrolmentRefused(RuntimeError):
    """An enrolment that would cross a tenant boundary or unbind an identity."""


class DeviceRevoked(RuntimeError):
    """A revoked device tried to write."""

    def __init__(self, device_id: str, revoked_at: str) -> None:
        super().__init__(f"device {device_id!r} was revoked at {revoked_at}")
        self.device_id = device_id
        self.revoked_at = revoked_at


class MalformedEntry(ValueError):
    """Wire data that does not meet the shape this store will persist.

    Raised rather than coerced: silent coercion of a seq or a payload is how a
    malformed batch becomes a plausible-looking row nobody questions.
    """


class SeqConflict(RuntimeError):
    """One seq offered twice by its own device with different content."""

    def __init__(self, device_id: str, seq: int, stored: str, offered: str) -> None:
        super().__init__(
            f"device {device_id!r} offered seq {seq} with entry_hash {offered!r} but "
            f"{stored!r} is already stored; refusing to treat conflicting evidence as redelivery"
        )
        self.device_id = device_id
        self.seq = seq
        self.stored_entry_hash = stored
        self.offered_entry_hash = offered


class EntryHashCollision(RuntimeError):
    """The same entry_hash arrived under two different devices."""

    def __init__(self, device_id: str, hashes: list[str]) -> None:
        super().__init__(
            f"device {device_id!r} shipped {len(hashes)} entry_hash value(s) already held by "
            "another device; refusing to treat this as redelivery"
        )
        self.device_id = device_id
        self.hashes = hashes


class UnknownDeviceError(RuntimeError):
    """A batch arrived for a device with no enrolment record.

    Refused rather than auto-enrolled: auto-enrolment would make the coverage
    denominator a function of who showed up, which is exactly the number this
    store exists to compute independently.
    """

    def __init__(self, device_id: str) -> None:
        super().__init__(f"no enrolment record for device {device_id!r}")
        self.device_id = device_id

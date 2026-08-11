"""Offline, independent verifier for a recorder ledger.

The verifier trusts nothing that the recorder wrote about itself. It re-derives
every ``entry_hash`` from the stored event data, walks the whole chain link by
link, re-checks every checkpoint signature against a supplied public key, and
reports missing sequence numbers and unpaired tool events.

It never opens a network connection and never writes to the ledger.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.schema import EventKind

GENESIS_PREV_HASH = "sha256:" + "00" * 32

# Columns that live on the ledger row itself rather than on the event.
_CHAIN_COLUMNS = frozenset({"prev_hash", "entry_hash", "event_digest"})


@dataclass
class VerifyReport:
    """Result of a full ledger verification pass."""

    ok: bool
    entries_checked: int
    broken_links: list[int]  # seqs where the re-derived chain did not hold
    bad_signatures: list[int]  # checkpoint seq_end values that failed
    gaps: list[tuple[int, int]]
    unpaired_tool_uses: list[str]  # tool_use_ids with no result — a FINDING
    orphan_tool_results: list[str]  # results with no tool_use — stronger finding

    # Descriptive context for the human report. Not part of the integrity
    # verdict; safe for consumers to ignore.
    device_ids: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    seq_start: int | None = None
    seq_end: int | None = None
    ts_first: str | None = None
    ts_last: str | None = None
    checkpoints_checked: int = 0
    signatures_verified: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["gaps"] = [list(g) for g in self.gaps]
        return data


class VerifyError(RuntimeError):
    """The ledger file could not be read at all."""


# --------------------------------------------------------------------------
# signature backend
# --------------------------------------------------------------------------


def _verify_signature(public_key_b64: str, data: bytes, sig: str) -> bool:
    """Ed25519 verification via the recorder's own key module.

    Imported lazily: ``keys.py`` needs ``cryptography``, which callers who
    verify without a public key (skipping signature checks entirely) should
    never be forced to install.
    """
    from byoai.recorder.keys import DeviceKey

    try:
        return bool(DeviceKey.verify(public_key_b64, data, sig))
    except Exception:
        return False


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def _connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise VerifyError(f"ledger not found: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.DatabaseError as exc:  # pragma: no cover - corrupt file
        raise VerifyError(f"cannot read table {table}: {exc}") from exc
    return [r["name"] for r in rows]


def _row_to_event_dict(row: sqlite3.Row, event_columns: Iterable[str]) -> dict[str, Any]:
    """Rebuild the event mapping exactly as the recorder hashed it."""
    event: dict[str, Any] = {}
    for name in event_columns:
        value = row[name]
        if name == "payload":
            event[name] = json.loads(value) if isinstance(value, str) else (value or {})
        else:
            event[name] = value
    return event


def _event_digest(event: dict[str, Any]) -> str:
    return sha256_hex(canonicalize(event))


def _entry_hash(prev_hash: str, seq: int, digest: str) -> str:
    return sha256_hex(canonicalize({"prev_hash": prev_hash, "seq": seq, "event_digest": digest}))


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------


def verify_ledger(path: str | Path, *, public_key_b64: str | None = None) -> VerifyReport:
    """Re-derive and check every hash, signature and sequence in the ledger."""
    conn = _connect(path)
    try:
        return _verify(conn, public_key_b64)
    finally:
        conn.close()


def _verify(conn: sqlite3.Connection, public_key_b64: str | None) -> VerifyReport:
    all_columns = _columns(conn, "agent_events")
    if not all_columns:
        raise VerifyError("ledger has no agent_events table")
    event_columns = [c for c in all_columns if c not in _CHAIN_COLUMNS]

    rows = conn.execute("SELECT * FROM agent_events ORDER BY seq ASC").fetchall()

    broken_links: list[int] = []
    gaps: list[tuple[int, int]] = []
    device_ids: list[str] = []
    session_ids: list[str] = []
    notes: list[str] = []

    tool_uses: dict[str, int] = {}
    tool_results: dict[str, int] = {}

    prev_seq: int | None = None
    prev_stored_hash = GENESIS_PREV_HASH
    derived: dict[int, str] = {}
    ts_values: list[str] = []

    for row in rows:
        seq = int(row["seq"])

        if prev_seq is not None and seq != prev_seq + 1:
            gaps.append((prev_seq + 1, seq - 1))

        event = _row_to_event_dict(row, event_columns)
        stored_prev = row["prev_hash"]
        stored_entry = row["entry_hash"]

        # Link check uses the *stored* previous hash so that a single tampered
        # row is reported once, at its own seq, instead of cascading.
        derived_entry = _entry_hash(stored_prev, seq, _event_digest(event))
        derived[seq] = derived_entry

        if stored_prev != prev_stored_hash or derived_entry != stored_entry:
            broken_links.append(seq)

        prev_seq = seq
        prev_stored_hash = stored_entry

        dev = event.get("device_id")
        if dev and dev not in device_ids:
            device_ids.append(str(dev))
        ses = event.get("session_id")
        if ses and ses not in session_ids:
            session_ids.append(str(ses))
        ts = event.get("ts_device")
        if ts:
            ts_values.append(str(ts))

        kind = event.get("kind")
        kind = kind.value if isinstance(kind, EventKind) else kind
        tuid = event.get("tool_use_id")
        if tuid:
            if kind == EventKind.TOOL_USE.value and tuid not in tool_uses:
                tool_uses[str(tuid)] = seq
            elif kind == EventKind.TOOL_RESULT.value and tuid not in tool_results:
                tool_results[str(tuid)] = seq

    unpaired_tool_uses = sorted(t for t in tool_uses if t not in tool_results)
    orphan_tool_results = sorted(
        t
        for t, result_seq in tool_results.items()
        if t not in tool_uses or tool_uses[t] > result_seq
    )

    bad_signatures, checkpoints_checked, cp_notes = _verify_checkpoints(
        conn, derived, public_key_b64
    )
    notes.extend(cp_notes)

    ok = not (broken_links or bad_signatures or gaps or orphan_tool_results)

    return VerifyReport(
        ok=ok,
        entries_checked=len(rows),
        broken_links=broken_links,
        bad_signatures=bad_signatures,
        gaps=gaps,
        unpaired_tool_uses=unpaired_tool_uses,
        orphan_tool_results=orphan_tool_results,
        device_ids=device_ids,
        session_ids=session_ids,
        seq_start=int(rows[0]["seq"]) if rows else None,
        seq_end=int(rows[-1]["seq"]) if rows else None,
        ts_first=min(ts_values) if ts_values else None,
        ts_last=max(ts_values) if ts_values else None,
        checkpoints_checked=checkpoints_checked,
        signatures_verified=bool(public_key_b64) and checkpoints_checked > 0,
        notes=notes,
    )


def _checkpoint_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _columns(conn, "checkpoints")
    if not columns:
        return []
    rows = conn.execute("SELECT * FROM checkpoints").fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        # A ledger may store the checkpoint as a JSON blob or as columns.
        if "body" in columns and row["body"]:
            cp = json.loads(row["body"])
        elif "checkpoint" in columns and row["checkpoint"]:
            cp = json.loads(row["checkpoint"])
        else:
            cp = {k: row[k] for k in columns if k not in {"id", "rowid"}}
        out.append(cp)
    out.sort(key=lambda c: int(c.get("seq_end", 0)))
    return out


def _verify_checkpoints(
    conn: sqlite3.Connection,
    derived: dict[int, str],
    public_key_b64: str | None,
) -> tuple[list[int], int, list[str]]:
    bad: list[int] = []
    notes: list[str] = []
    try:
        checkpoints = _checkpoint_rows(conn)
    except (sqlite3.DatabaseError, ValueError) as exc:
        return [], 0, [f"checkpoint table unreadable: {exc}"]

    if not checkpoints:
        return [], 0, ["no checkpoints present in this ledger"]

    if public_key_b64 is None:
        notes.append(
            f"{len(checkpoints)} checkpoint(s) present but NOT signature-checked "
            "— rerun with --pubkey to verify device signatures"
        )

    for cp in checkpoints:
        seq_end = int(cp.get("seq_end", -1))
        failed = False

        sig = cp.get("sig")
        if public_key_b64 is not None:
            unsigned = {k: v for k, v in cp.items() if k != "sig"}
            if not isinstance(sig, str) or not _verify_signature(
                public_key_b64, canonicalize(unsigned), sig
            ):
                failed = True
                notes.append(f"checkpoint ending at seq {seq_end}: signature does not verify")
        elif not isinstance(sig, str):
            failed = True
            notes.append(f"checkpoint ending at seq {seq_end}: missing signature")

        head = cp.get("chain_head")
        expected = derived.get(seq_end)
        if expected is None:
            failed = True
            notes.append(f"checkpoint ending at seq {seq_end}: no such entry in the ledger")
        elif head != expected:
            failed = True
            notes.append(
                f"checkpoint ending at seq {seq_end}: chain head does not match the "
                "hash re-derived from the stored entries"
            )

        if failed:
            bad.append(seq_end)

    return bad, len(checkpoints), notes

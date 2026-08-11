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
from byoai.recorder.ledger import compute_entry_hash
from byoai.recorder.merkle import (
    InclusionProof,
    ProofStep,
    checkpoint_leaf_hash,
    verify_inclusion,
)
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
    # One dict per KEY_ROTATED event found: old/new device_id, reason,
    # whether the cross-signature verified (None if no pubkey was supplied
    # for the old device, same "not checked" convention as checkpoints).
    key_rotations: list[dict[str, Any]] = field(default_factory=list)
    # seqs where an entry is attributed to a device whose key was already
    # rotated out as of that entry's effective_epoch — a genuine finding,
    # distinct from the device_id simply differing across a legitimate
    # rotation boundary.
    stale_key_usage: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["gaps"] = [list(g) for g in self.gaps]
        return data


class VerifyError(RuntimeError):
    """The ledger file could not be read at all."""


def verify_checkpoint_epoch_inclusion(
    checkpoint: dict[str, Any], proof: InclusionProof, epoch_root: bytes
) -> bool:
    """The missing link in spec section 6.3's verification path: proves
    ``checkpoint`` (already verified against the device chain and its own
    signature by :func:`_verify_checkpoints` / the caller) was actually
    included in the tenant epoch tree that ``epoch_root`` is the root of.

    Re-derives the leaf hash from ``checkpoint`` itself rather than trusting
    ``proof.leaf_hash`` — a proof carrying a leaf hash that doesn't match the
    checkpoint it claims to cover is exactly the kind of substitution this
    check exists to catch. No network access; the caller supplies the
    checkpoint (from the local ledger), the proof, and the root (both
    presumably from an exported bundle, once one exists).
    """
    if checkpoint_leaf_hash(checkpoint) != proof.leaf_hash:
        return False
    if proof.root != epoch_root:
        return False
    return verify_inclusion(proof)


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


def _check_rotation(
    payload: dict[str, Any], device_public_keys: dict[str, str]
) -> dict[str, Any]:
    """Verify a KEY_ROTATED event's cross-signature, if a pubkey is available."""
    old_id = payload.get("old_device_id")
    new_id = payload.get("new_device_id")
    new_public_key = payload.get("new_public_key")
    cross_signature = payload.get("cross_signature")

    verified: bool | None = None
    old_pubkey = device_public_keys.get(str(old_id)) if old_id else None
    if old_pubkey is not None:
        signed_fields = {
            "old_device_id": old_id,
            "new_device_id": new_id,
            "new_public_key": new_public_key,
        }
        verified = bool(
            isinstance(cross_signature, str)
            and _verify_signature(old_pubkey, canonicalize(signed_fields), cross_signature)
        )

    return {
        "old_device_id": old_id,
        "new_device_id": new_id,
        "reason": payload.get("reason"),
        "effective_epoch": payload.get("effective_epoch"),
        "cross_signature_verified": verified,
    }


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------


def verify_ledger(
    path: str | Path,
    *,
    public_key_b64: str | None = None,
    device_public_keys: dict[str, str] | None = None,
) -> VerifyReport:
    """Re-derive and check every hash, signature and sequence in the ledger.

    ``public_key_b64`` checks checkpoint signatures, as before.
    ``device_public_keys`` (device_id -> base64 Ed25519 public key) is used
    to verify ``KEY_ROTATED`` cross-signatures against the *old* device's
    key; a rotation whose old device_id has no entry there is noted as
    unchecked rather than failed, matching the existing checkpoint
    convention.
    """
    conn = _connect(path)
    try:
        return _verify(conn, public_key_b64, device_public_keys or {})
    finally:
        conn.close()


@dataclass
class _ChainWalk:
    """Everything a walk over normalized chain entries accumulates, shared
    between the SQLite-backed ledger path and the JSON-bundle path so the two
    can never silently drift apart on what counts as a finding."""

    entries_checked: int
    broken_links: list[int]
    gaps: list[tuple[int, int]]
    device_ids: list[str]
    session_ids: list[str]
    ts_values: list[str]
    unpaired_tool_uses: list[str]
    orphan_tool_results: list[str]
    key_rotations: list[dict[str, Any]]
    stale_key_usage: list[int]
    derived: dict[int, str]
    notes: list[str]


def _walk_chain(
    entries: Iterable[dict[str, Any]], device_public_keys: dict[str, str]
) -> _ChainWalk:
    """Re-derive and check the hash chain over normalized entries.

    Each item in ``entries`` must have ``seq`` (int), ``prev_hash`` (str),
    ``entry_hash`` (str) and ``event`` (the event mapping, already parsed) —
    the same shape whether it came from an ``agent_events`` row or a bundle's
    ``entries`` array. This is the one chain-walking implementation; both
    :func:`verify_ledger` and :func:`verify_bundle` call it so a finding in
    one can never fail to appear in the other.
    """
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

    key_rotations: list[dict[str, Any]] = []
    stale_key_usage: list[int] = []
    # device_id of a retired key -> effective_epoch (RFC3339 str) after which
    # that device_id is no longer a valid signer.
    retired_devices: dict[str, str] = {}

    entries_checked = 0
    for entry in entries:
        entries_checked += 1
        seq = int(entry["seq"])

        # The chain is defined to start at seq 1, so a missing prefix is a
        # gap too, not just a hole between consecutive rows (matches
        # Ledger.missing_ranges()'s definition of a gap).
        if prev_seq is None and seq > 1:
            gaps.append((1, seq - 1))
        elif prev_seq is not None and seq != prev_seq + 1:
            gaps.append((prev_seq + 1, seq - 1))

        event = entry["event"]
        stored_prev = entry["prev_hash"]
        stored_entry = entry["entry_hash"]

        # Link check uses the *stored* previous hash so that a single tampered
        # row is reported once, at its own seq, instead of cascading.
        derived_entry = compute_entry_hash(stored_prev, seq, _event_digest(event))
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

        # A device_id change immediately after a KEY_ROTATED event is the
        # *point* of cross-signing continuity, not tampering — so this check
        # only flags entries still attributed to a device whose key was
        # already retired as of its own effective_epoch.
        if dev and dev in retired_devices and ts and str(ts) >= retired_devices[dev]:
            stale_key_usage.append(seq)

        if kind == EventKind.KEY_ROTATED.value:
            rotation = _check_rotation(event.get("payload") or {}, device_public_keys)
            key_rotations.append(rotation)
            old_id = rotation.get("old_device_id")
            epoch = rotation.get("effective_epoch")
            if old_id and epoch:
                retired_devices[str(old_id)] = str(epoch)

    unpaired_tool_uses = sorted(t for t in tool_uses if t not in tool_results)
    orphan_tool_results = sorted(
        t
        for t, result_seq in tool_results.items()
        if t not in tool_uses or tool_uses[t] > result_seq
    )

    for rotation in key_rotations:
        old_id = rotation.get("old_device_id")
        seq_note = f"key rotation {old_id} -> {rotation.get('new_device_id')}"
        if rotation["cross_signature_verified"] is None:
            notes.append(
                f"{seq_note}: cross-signature NOT checked — rerun with the old "
                "device's public key to verify continuity"
            )
        elif not rotation["cross_signature_verified"]:
            notes.append(f"{seq_note}: cross-signature does NOT verify")

    return _ChainWalk(
        entries_checked=entries_checked,
        broken_links=broken_links,
        gaps=gaps,
        device_ids=device_ids,
        session_ids=session_ids,
        ts_values=ts_values,
        unpaired_tool_uses=unpaired_tool_uses,
        orphan_tool_results=orphan_tool_results,
        key_rotations=key_rotations,
        stale_key_usage=stale_key_usage,
        derived=derived,
        notes=notes,
    )


def _verify(
    conn: sqlite3.Connection,
    public_key_b64: str | None,
    device_public_keys: dict[str, str],
) -> VerifyReport:
    all_columns = _columns(conn, "agent_events")
    if not all_columns:
        raise VerifyError("ledger has no agent_events table")
    event_columns = [c for c in all_columns if c not in _CHAIN_COLUMNS]

    rows = conn.execute("SELECT * FROM agent_events ORDER BY seq ASC").fetchall()
    entries = [
        {
            "seq": row["seq"],
            "prev_hash": row["prev_hash"],
            "entry_hash": row["entry_hash"],
            "event": _row_to_event_dict(row, event_columns),
        }
        for row in rows
    ]

    walk = _walk_chain(entries, device_public_keys)

    try:
        checkpoints = _checkpoint_rows(conn)
        checkpoint_notes: list[str] = []
    except (sqlite3.DatabaseError, ValueError) as exc:
        checkpoints = []
        checkpoint_notes = [f"checkpoint table unreadable: {exc}"]

    if checkpoint_notes:
        bad_signatures: list[int] = []
        checkpoints_checked = 0
        cp_notes = checkpoint_notes
    else:
        bad_signatures, checkpoints_checked, cp_notes = _verify_checkpoints(
            checkpoints, walk.derived, public_key_b64
        )

    notes = [*walk.notes, *cp_notes]

    forged_rotations = [
        r for r in walk.key_rotations if r["cross_signature_verified"] is False
    ]

    ok = not (
        walk.broken_links
        or bad_signatures
        or walk.gaps
        or walk.orphan_tool_results
        or walk.stale_key_usage
        or forged_rotations
    )

    return VerifyReport(
        ok=ok,
        entries_checked=walk.entries_checked,
        broken_links=walk.broken_links,
        bad_signatures=bad_signatures,
        gaps=walk.gaps,
        unpaired_tool_uses=walk.unpaired_tool_uses,
        orphan_tool_results=walk.orphan_tool_results,
        device_ids=walk.device_ids,
        session_ids=walk.session_ids,
        seq_start=int(entries[0]["seq"]) if entries else None,
        seq_end=int(entries[-1]["seq"]) if entries else None,
        ts_first=min(walk.ts_values) if walk.ts_values else None,
        ts_last=max(walk.ts_values) if walk.ts_values else None,
        checkpoints_checked=checkpoints_checked,
        signatures_verified=bool(public_key_b64) and checkpoints_checked > 0,
        notes=notes,
        key_rotations=walk.key_rotations,
        stale_key_usage=walk.stale_key_usage,
    )


def _checkpoint_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _columns(conn, "checkpoints")
    if not columns:
        return []
    rows = conn.execute("SELECT * FROM checkpoints").fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        # The only schema this codebase writes stores each checkpoint as a
        # JSON blob in a `body` column (see ledger.py's _SCHEMA). Fall back
        # to reconstructing one from raw columns only for a ledger written
        # by some other schema shape, not a case any writer here produces.
        if "body" in columns and row["body"]:
            cp = json.loads(row["body"])
        else:
            cp = {k: row[k] for k in columns if k not in {"id", "rowid"}}
        out.append(cp)
    out.sort(key=lambda c: int(c.get("seq_end", 0)))
    return out


def _verify_checkpoints(
    checkpoints: list[dict[str, Any]],
    derived: dict[int, str],
    public_key_b64: str | None,
) -> tuple[list[int], int, list[str]]:
    bad: list[int] = []
    notes: list[str] = []

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


_EPOCH_SIGNED_FIELDS = ("epoch_index", "root", "epoch_start", "epoch_end", "tenant_id")


def _verify_epoch_signature(epoch: dict[str, Any]) -> bool | None:
    """Check an epoch root's tenant-KMS signature (spec §6.2 level 3).

    Returns ``None`` — not checked, not failed — when the bundle carries no
    ``tenant_sig``/``tenant_kms_public_key_b64`` for this epoch, matching the
    existing "not checked" convention used for checkpoints without a
    supplied public key. Uses the same Ed25519 primitive as everything else
    in this codebase; a tenant KMS that signs with something else needs its
    own verifier, not a change here.
    """
    sig = epoch.get("tenant_sig")
    public_key_b64 = epoch.get("tenant_kms_public_key_b64")
    if not isinstance(sig, str) or not isinstance(public_key_b64, str):
        return None
    signed_fields = {k: epoch.get(k) for k in _EPOCH_SIGNED_FIELDS}
    return _verify_signature(public_key_b64, canonicalize(signed_fields), sig)


def _verify_anchor(
    anchor_type: str,
    receipt: dict[str, Any],
    epoch: dict[str, Any],
    rekor_public_key_b64: str | None,
) -> tuple[bool, list[str]]:
    """Dispatch to the right external anchor verifier (spec §6.2 level 4).

    Imported lazily for the same reason ``_verify_signature`` imports
    ``keys`` lazily: callers who never verify anchors shouldn't be forced to
    install ``rfc3161ng``.
    """
    from byoai.recorder import anchor as anchor_module

    try:
        epoch_root = bytes.fromhex(epoch["root"])
    except (KeyError, ValueError) as exc:
        return False, [f"cannot verify anchor: epoch root is missing/malformed ({exc})"]

    if anchor_type == "rfc3161_tsa":
        return anchor_module.verify_rfc3161_receipt(receipt, epoch_root)
    if anchor_type == "sigstore_rekor":
        inclusion = receipt.get("inclusion_proof") or {}
        flat_receipt = {
            **inclusion,
            "signed_entry_timestamp": receipt.get("signed_entry_timestamp"),
        }
        return anchor_module.verify_rekor_receipt(
            flat_receipt, epoch_root, rekor_public_key_b64=rekor_public_key_b64
        )
    return False, [f"unknown anchor type {anchor_type!r} — cannot verify"]


@dataclass
class BundleVerifyReport:
    """Result of verifying an examiner export bundle (spec §10.3), covering
    all five steps of the sketch in
    ``internal_doc/recorder_contract_export_bundle.md``: chain,
    checkpoint signatures, checkpoint-to-epoch inclusion, tenant epoch-root
    signature, and (when ``check_anchors`` is set) the external anchor
    receipt. An anchor of type ``"none"`` is legitimately unanchored, not a
    failure — see :func:`verify_bundle`."""

    ok: bool
    entries_checked: int
    broken_links: list[int]
    bad_checkpoint_signatures: list[int]
    bad_inclusions: list[int]  # checkpoint seq_end values whose proof did not verify
    bad_epoch_signatures: list[int]  # epoch_index values whose tenant_sig did not verify
    gaps: list[tuple[int, int]]
    unpaired_tool_uses: list[str]
    orphan_tool_results: list[str]
    device_ids: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    checkpoints_checked: int = 0
    inclusions_checked: int = 0
    epoch_signatures_checked: int = 0
    anchors_checked: int = 0
    bad_anchors: list[int] = field(default_factory=list)  # epoch_index values whose anchor failed
    key_rotations: list[dict[str, Any]] = field(default_factory=list)
    stale_key_usage: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["gaps"] = [list(g) for g in self.gaps]
        return data


def verify_bundle(
    bundle: dict[str, Any],
    *,
    device_public_keys: dict[str, str] | None = None,
    check_anchors: bool = True,
    rekor_public_key_b64: str | None = None,
) -> BundleVerifyReport:
    """Offline verification of an examiner export bundle (spec §10.3).

    Re-runs the same chain walk as :func:`verify_ledger`, over
    ``bundle["entries"]`` instead of a SQLite cursor, then re-checks every
    checkpoint's signature, its inclusion proof against ``bundle["epochs"]``,
    each epoch's tenant signature, and — when ``check_anchors`` is set and an
    epoch's ``anchor.type`` is not ``"none"`` — that epoch's external anchor
    receipt (RFC 3161 TSA or Sigstore Rekor, via ``anchor.py``). This covers
    all five steps of the verification path in spec §6.3. An epoch with
    ``anchor.type == "none"`` is legitimately unanchored and is noted as such,
    never silently treated as passing.
    """
    public_key_b64 = bundle.get("device", {}).get("public_key_b64")
    entries = bundle.get("entries", [])

    walk = _walk_chain(entries, device_public_keys or {})

    bundle_checkpoints = bundle.get("checkpoints", [])
    checkpoints = [bc["checkpoint"] for bc in bundle_checkpoints]
    bad_signatures, checkpoints_checked, cp_notes = _verify_checkpoints(
        checkpoints, walk.derived, public_key_b64
    )

    epoch_roots = {
        epoch["epoch_index"]: bytes.fromhex(epoch["root"])
        for epoch in bundle.get("epochs", [])
    }

    bad_inclusions: list[int] = []
    inclusions_checked = 0
    notes = [*walk.notes, *cp_notes]

    for bc in bundle_checkpoints:
        proof_json = bc.get("inclusion_proof")
        epoch_index = bc.get("epoch_index")
        seq_end = int(bc["checkpoint"].get("seq_end", -1))

        if proof_json is None or epoch_index is None:
            notes.append(
                f"checkpoint ending at seq {seq_end}: not yet anchored to an "
                "epoch — inclusion not checked"
            )
            continue

        root = epoch_roots.get(epoch_index)
        if root is None:
            bad_inclusions.append(seq_end)
            notes.append(
                f"checkpoint ending at seq {seq_end}: references epoch "
                f"{epoch_index}, which is not present in this bundle's epochs"
            )
            continue

        inclusions_checked += 1
        proof = InclusionProof(
            leaf_index=proof_json["leaf_index"],
            leaf_hash=checkpoint_leaf_hash(bc["checkpoint"]),
            steps=tuple(
                ProofStep(sibling=bytes.fromhex(step["sibling"]), side=step["side"])
                for step in proof_json["steps"]
            ),
            root=root,
        )
        if not verify_checkpoint_epoch_inclusion(bc["checkpoint"], proof, root):
            bad_inclusions.append(seq_end)
            notes.append(f"checkpoint ending at seq {seq_end}: inclusion proof does not verify")

    bad_epoch_signatures: list[int] = []
    epoch_signatures_checked = 0
    bad_anchors: list[int] = []
    anchors_checked = 0
    for epoch in bundle.get("epochs", []):
        epoch_index = epoch["epoch_index"]
        verified = _verify_epoch_signature(epoch)
        if verified is None:
            notes.append(
                f"epoch {epoch_index}: tenant signature NOT checked — bundle carries "
                "no tenant_sig/tenant_kms_public_key_b64 for it"
            )
        else:
            epoch_signatures_checked += 1
            if not verified:
                bad_epoch_signatures.append(epoch_index)
                notes.append(f"epoch {epoch_index}: tenant signature does NOT verify")

        anchor = epoch.get("anchor") or {}
        anchor_type = anchor.get("type", "none")
        if anchor_type == "none":
            continue
        if not check_anchors:
            notes.append(
                f"epoch {epoch_index}: anchor receipt ({anchor_type}) NOT verified — "
                "check_anchors=False"
            )
            continue

        anchor_ok, anchor_notes = _verify_anchor(
            anchor_type, anchor.get("receipt") or {}, epoch, rekor_public_key_b64
        )
        anchors_checked += 1
        notes.extend(f"epoch {epoch_index}: anchor — {n}" for n in anchor_notes)
        if not anchor_ok:
            bad_anchors.append(epoch_index)

    forged_rotations = [r for r in walk.key_rotations if r["cross_signature_verified"] is False]

    ok = not (
        walk.broken_links
        or bad_signatures
        or bad_inclusions
        or bad_epoch_signatures
        or bad_anchors
        or walk.gaps
        or walk.orphan_tool_results
        or walk.stale_key_usage
        or forged_rotations
    )

    return BundleVerifyReport(
        ok=ok,
        entries_checked=walk.entries_checked,
        broken_links=walk.broken_links,
        bad_checkpoint_signatures=bad_signatures,
        bad_inclusions=bad_inclusions,
        bad_epoch_signatures=bad_epoch_signatures,
        anchors_checked=anchors_checked,
        bad_anchors=bad_anchors,
        gaps=walk.gaps,
        unpaired_tool_uses=walk.unpaired_tool_uses,
        orphan_tool_results=walk.orphan_tool_results,
        device_ids=walk.device_ids,
        session_ids=walk.session_ids,
        checkpoints_checked=checkpoints_checked,
        inclusions_checked=inclusions_checked,
        epoch_signatures_checked=epoch_signatures_checked,
        key_rotations=walk.key_rotations,
        stale_key_usage=walk.stale_key_usage,
        notes=notes,
    )

"""Regenerate the test vectors printed in docs/seal-format.md.

Run from the repository root with the recorder extra installed:

    pip install -e ".[recorder]"
    python docs/seal-format-vectors.py

Everything here goes through the real library (Ledger, Checkpointer,
build_epoch_tree, verify_ledger, verify_bundle, verify_rekor_receipt), with
fixed keys, ids and clocks so the output is byte-for-byte reproducible.
Ed25519 signatures are deterministic (RFC 8032), so fixed keys give fixed
signatures.

The keys below are derived from public strings. They are test keys: never
use them for anything real.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from byoai.recorder.anchor import verify_rekor_receipt
from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.checkpoint import Checkpointer, checkpoint_signing_bytes
from byoai.recorder.keys import DeviceKey
from byoai.recorder.ledger import GENESIS_PREV_HASH, Ledger, compute_entry_hash
from byoai.recorder.merkle import (
    MerkleTree,
    _leaf_hash,
    build_epoch_tree,
    checkpoint_leaf_hash,
)
from byoai.recorder.schema import EVENT_SCHEMA_VERSION, AgentEvent, event_digest
from byoai.recorder.verify import verify_bundle, verify_ledger


def test_key(label: str) -> DeviceKey:
    seed = hashlib.sha256(label.encode("utf-8")).digest()
    return DeviceKey(Ed25519PrivateKey.from_private_bytes(seed))


def show(label: str, value: object) -> None:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    print(f"{label}:\n  {value}")


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# --------------------------------------------------------------------------
# Vector 0: canonical JSON (RFC 8785)
# --------------------------------------------------------------------------

section("Vector 0: canonical JSON")
sample = {
    "z": 1.0,
    "a": [1e21, 0.1, -0.0, 1e-7],
    "é": "café\n ",
    "\U0001f600": "astral key",
    "ﬁ": "BMP key above the surrogate range",
    "n": None,
    "t": True,
}
show("input (Python repr)", repr(sample))
show("canonical bytes (UTF-8, shown as text)", canonicalize(sample))
show("canonical bytes (hex)", canonicalize(sample).hex())
show("sha256", sha256_hex(canonicalize(sample)))


# --------------------------------------------------------------------------
# Vector 1: three events, chain links, one checkpoint per event
# --------------------------------------------------------------------------

section("Vector 1: device key, three events, hash chain")

device = test_key("byoai seal-format test vector: device key")
show("device private seed (hex, TEST ONLY)",
     hashlib.sha256(b"byoai seal-format test vector: device key").hexdigest())
show("device public_key_b64", device.public_key_b64)
show("device_id", device.device_id)

payloads = [
    {"role": "user", "text": "list files in /tmp"},
    {"input": {"path": "/tmp"}},
    {"is_error": False, "content": "a.txt\nb.txt", "bytes": 12, "ratio": 0.5},
]
specs = [
    ("message", None, None),
    ("tool_use", "toolu_01", "list_dir"),
    ("tool_result", "toolu_01", "list_dir"),
]

tmp = Path(tempfile.mkdtemp(prefix="seal-vectors-"))
ledger = Ledger(tmp / "ledger.db", device.device_id)
# every_events=1: one checkpoint per event, so vector 2 has three leaves
# (an odd count, which is the case where Merkle constructions differ).
wall = [1785000000.0]
cp = Checkpointer(
    ledger, device, every_events=1,
    monotonic=lambda: 0.0, wall_clock=lambda: wall[0],
)
checkpoints = []
for i, ((kind, tuid, tname), payload) in enumerate(zip(specs, payloads, strict=True), 1):
    event = AgentEvent(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id=f"evt_{i:032x}",
        device_id=device.device_id,
        session_id="sess_vectors",
        seq=0,  # the ledger stamps the real seq
        kind=kind,
        ts_device=f"2026-07-25T12:00:0{i}.000000Z",
        ts_monotonic_ns=1_000_000 * i,
        tool_use_id=tuid,
        tool_name=tname,
        payload=payload,
        payload_hash=sha256_hex(canonicalize(payload)),
        model="claude-test",
        provider="anthropic",
        trace_id="tr_" + "a" * 32,
        span_id="sp_" + "b" * 32,
        parent_span_id=None,
        continues_from=None,
    )
    entry = ledger.append(event)
    assert entry is not None
    digest_input = canonicalize(entry.event.to_dict())
    link_input = canonicalize(
        {"prev_hash": entry.prev_hash, "seq": entry.seq, "event_digest": entry.event_digest}
    )
    assert entry.event_digest == event_digest(entry.event) == sha256_hex(digest_input)
    assert entry.entry_hash == compute_entry_hash(entry.prev_hash, entry.seq, entry.event_digest)
    print(f"\n--- entry seq={entry.seq} ---")
    show("event canonical bytes", digest_input)
    show("payload_hash = sha256(canonical(payload))", entry.event.payload_hash)
    show("event_digest = sha256(event canonical bytes)", entry.event_digest)
    show("prev_hash", entry.prev_hash)
    show("link canonical bytes", link_input)
    show("entry_hash = sha256(link canonical bytes)", entry.entry_hash)

    wall[0] += 1.0
    emitted = cp.note(entry.seq)
    assert emitted is not None
    checkpoints.append(emitted)

assert checkpoints[0]["chain_head"] != GENESIS_PREV_HASH

section("Vector 2: checkpoints, epoch Merkle tree (3 leaves), inclusion proofs")

for c in checkpoints:
    print(f"\n--- checkpoint seq_end={c['seq_end']} ---")
    show("signing bytes = canonical(checkpoint minus sig)", checkpoint_signing_bytes(c))
    show("sig", c["sig"])
    show("full checkpoint canonical bytes (Merkle leaf input)", canonicalize(c))
    show("leaf hash = sha256(0x00 || leaf input) (hex)", checkpoint_leaf_hash(c).hex())

root, proofs = build_epoch_tree(checkpoints)
leaves = [checkpoint_leaf_hash(c) for c in checkpoints]
tree = MerkleTree(leaves)
print()
show("node(0,1) = sha256(0x01 || leaf0 || leaf1) (hex)", tree._levels[1][0].hex())
show("level 1 (leaf 2 promoted unchanged)", [h.hex() for h in tree._levels[1]])
show("epoch root (hex)", root.hex())
for p in proofs:
    show(
        f"inclusion proof leaf_index={p.leaf_index}",
        [{"sibling": s.sibling.hex(), "side": s.side} for s in p.steps],
    )

section("Vector 3: verify_ledger and verify_bundle over the above")

report = verify_ledger(tmp / "ledger.db", public_key_b64=device.public_key_b64)
show("verify_ledger ok", report.ok)
show("verify_ledger checkpoints_checked", report.checkpoints_checked)
show("verify_ledger unpaired / orphan", (report.unpaired_tool_uses, report.orphan_tool_results))

tenant = test_key("byoai seal-format test vector: tenant key")
epoch = {
    "epoch_index": 0,
    "root": root.hex(),
    "epoch_start": "2026-07-25T12:00:00.000000Z",
    "epoch_end": "2026-07-25T12:10:00.000000Z",
    "tenant_id": "ten_vectors",
    "tenant_kms_public_key_b64": tenant.public_key_b64,
}
epoch_signed = {
    k: epoch[k] for k in ("epoch_index", "root", "epoch_start", "epoch_end", "tenant_id")
}
epoch["tenant_sig"] = tenant.sign(canonicalize(epoch_signed))
epoch["anchor"] = {"type": "none", "receipt": None}
show("tenant public_key_b64", tenant.public_key_b64)
show("epoch signing bytes", canonicalize(epoch_signed))
show("tenant_sig", epoch["tenant_sig"])

bundle = {
    "bundle_version": "1.0",
    "device": {"device_id": device.device_id, "public_key_b64": device.public_key_b64},
    "entries": [
        {
            "seq": e.seq,
            "prev_hash": e.prev_hash,
            "entry_hash": e.entry_hash,
            "event_digest": e.event_digest,
            "event": e.event.to_dict(),
        }
        for e in ledger.iter_entries()
    ],
    "checkpoints": [
        {
            "checkpoint": c,
            "epoch_index": 0,
            "inclusion_proof": {
                "leaf_index": p.leaf_index,
                "steps": [{"sibling": s.sibling.hex(), "side": s.side} for s in p.steps],
            },
        }
        for c, p in zip(checkpoints, proofs, strict=True)
    ],
    "epochs": [epoch],
}
breport = verify_bundle(bundle)
show("verify_bundle ok", breport.ok)
show("verify_bundle inclusions_checked / epoch_signatures_checked",
     (breport.inclusions_checked, breport.epoch_signatures_checked))
show("bundle sha256 (canonical, for comparing your own copy)", sha256_hex(canonicalize(bundle)))

# Tamper: change one payload byte in entry 2 without fixing hashes.
bundle["entries"][1]["event"]["payload"]["input"]["path"] = "/tmq"
treport = verify_bundle(bundle)
show("after tampering entry seq=2: ok", treport.ok)
show("after tampering entry seq=2: broken_links", treport.broken_links)
show("after tampering entry seq=2: bad_checkpoint_signatures", treport.bad_checkpoint_signatures)

section("Vector 4: KEY_ROTATED cross-signature")

new_device = test_key("byoai seal-format test vector: rotated-in key")
rot_fields = {
    "old_device_id": device.device_id,
    "new_device_id": new_device.device_id,
    "new_public_key": new_device.public_key_b64,
}
show("new public_key_b64", new_device.public_key_b64)
show("new device_id", new_device.device_id)
show("cross-signature signing bytes", canonicalize(rot_fields))
show("cross_signature (signed by OLD key)", device.sign(canonicalize(rot_fields)))

section("Vector 5: Rekor-style receipt over the epoch root (RFC 6962 audit path)")

# The epoch root is one leaf (index 2) of a 5-leaf log. Other leaves are
# arbitrary fixed bytes.
log_leaves_data = [b"log leaf 0", b"log leaf 1", root, b"log leaf 3", b"log leaf 4"]
log_leaf_hashes = [_leaf_hash(d) for d in log_leaves_data]


def mth(hs: list[bytes]) -> bytes:
    return MerkleTree(hs).root


def audit_path(m: int, hs: list[bytes]) -> list[bytes]:
    """RFC 6962 section 2.1.1 PATH(m, D[n]), leaf-to-root order."""
    n = len(hs)
    if n == 1:
        return []
    k = 1
    while k * 2 < n:
        k *= 2
    if m < k:
        return audit_path(m, hs[:k]) + [mth(hs[k:])]
    return audit_path(m - k, hs[k:]) + [mth(hs[:k])]


rekor_log = test_key("byoai seal-format test vector: rekor log key")
receipt = {
    "log_index": 2,
    "tree_size": 5,
    "root_hash": mth(log_leaf_hashes).hex(),
    "hashes": [h.hex() for h in audit_path(2, log_leaf_hashes)],
}
set_fields = {k: receipt[k] for k in ("log_index", "tree_size", "root_hash")}
receipt["signed_entry_timestamp"] = rekor_log.sign(canonicalize(set_fields))
show("log public_key_b64", rekor_log.public_key_b64)
show("receipt (flat form verify_rekor_receipt takes)", receipt)
show("SET signing bytes", canonicalize(set_fields))
ok, notes = verify_rekor_receipt(receipt, root, rekor_public_key_b64=rekor_log.public_key_b64)
show("verify_rekor_receipt", (ok, notes))

ledger.close()
shutil.rmtree(tmp)

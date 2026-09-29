"""End-to-end test of verify_bundle() against a bundle assembled from a real
Ledger + Checkpointer + MockCoriqo epoch — steps 1-3 of the examiner export
bundle verification path in ``docs/seal-format.md`` §11.

The bundle-assembly helper here is deliberately test-only (mirrors
``mock_coriqo.py``'s own "not real Coriqo" convention): producing a bundle is
server-side/proprietary per spec §15.6, this file only needs *a* bundle to
verify against.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from tests.recorder.conftest import asgi_client
from tests.recorder.mock_coriqo import Epoch, MockCoriqo
from tests.recorder.test_epoch_merkle import (
    CORIQO_BASE_URL,
    emit_checkpoint,
    enroll_device,
)

from byoai.recorder.canonical import canonicalize
from byoai.recorder.keys import DeviceKey
from byoai.recorder.ledger import Ledger
from byoai.recorder.merkle import checkpoint_leaf_hash
from byoai.recorder.shipper import Shipper
from byoai.recorder.verify import verify_bundle

_RFC3161_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "rfc3161"
_RFC3161_FIXTURE_EPOCH_ROOT = hashlib.sha256(b"fake epoch root").digest()


def _client(mock: MockCoriqo):
    return asgi_client(mock, base_url=CORIQO_BASE_URL)


def _sign_epoch(tenant_key: DeviceKey, epoch: dict) -> str:
    signed_fields = {
        k: epoch[k] for k in ("epoch_index", "root", "epoch_start", "epoch_end", "tenant_id")
    }
    return tenant_key.sign(canonicalize(signed_fields))


def _build_bundle(
    ledger: Ledger,
    public_key_b64: str,
    mock: MockCoriqo,
    epoch: Epoch,
    *,
    tenant_key: DeviceKey | None = None,
) -> dict:
    entries = [
        {
            "seq": e.seq,
            "prev_hash": e.prev_hash,
            "entry_hash": e.entry_hash,
            "event": e.event.to_dict(),
        }
        for e in ledger.iter_entries()
    ]

    device_id = ledger.device_id
    checkpoints = []
    for cp in mock.checkpoints_for(device_id):
        proof = None
        if cp.epoch_index is not None:
            found = epoch.proof_for(device_id, cp.seq_end)
            if found is not None:
                assert checkpoint_leaf_hash(cp.body) == found.leaf_hash
                proof = {
                    "leaf_index": found.leaf_index,
                    "steps": [
                        {"sibling": step.sibling.hex(), "side": step.side}
                        for step in found.steps
                    ],
                }
        checkpoints.append(
            {"checkpoint": cp.body, "epoch_index": cp.epoch_index, "inclusion_proof": proof}
        )

    return {
        "bundle_version": "1.0",
        "device": {"device_id": device_id, "public_key_b64": public_key_b64, "key_history": []},
        "entries": entries,
        "checkpoints": checkpoints,
        "epochs": [_build_epoch_dict(epoch, tenant_key)],
        "range": {"seq_start": entries[0]["seq"] if entries else None},
    }


def _build_epoch_dict(epoch: Epoch, tenant_key: DeviceKey | None) -> dict:
    epoch_dict = {
        "epoch_index": epoch.index,
        "root": epoch.root.hex(),
        "epoch_start": "2026-08-11T00:00:00.000000Z",
        "epoch_end": "2026-08-11T00:10:00.000000Z",
        "tenant_id": "ten_test",
        "anchor": {"type": "none", "receipt": None},
    }
    if tenant_key is not None:
        epoch_dict["tenant_kms_public_key_b64"] = tenant_key.public_key_b64
        epoch_dict["tenant_sig"] = _sign_epoch(tenant_key, epoch_dict)
    return epoch_dict


def _build_ledger_bundle(
    tmp_path: Path, *, n_events: int = 1, tenant_key: DeviceKey | None = None
) -> dict:
    """Enroll a device, emit a checkpoint, ship it, build its epoch, and
    assemble an examiner export bundle — the setup shared by every test in
    this file that needs *a* real, internally-consistent bundle to tamper
    with or verify.
    """
    mock = MockCoriqo()
    ledger, key, device_id = enroll_device(tmp_path, mock)
    try:
        emit_checkpoint(ledger, key, n_events=n_events)
        shipper = Shipper(
            ledger, key, coriqo_base_url=CORIQO_BASE_URL, http_client=_client(mock)
        )
        try:
            shipper.ship_checkpoints_once()
        finally:
            shipper.close()

        epoch = mock.build_epoch()
        assert epoch is not None

        return _build_bundle(ledger, key.public_key_b64, mock, epoch, tenant_key=tenant_key)
    finally:
        ledger.close()


def test_verify_bundle_accepts_a_valid_bundle(tmp_path):
    bundle = _build_ledger_bundle(tmp_path, n_events=3)

    report = verify_bundle(bundle)
    assert report.ok, report.notes
    assert report.entries_checked == 3
    assert report.checkpoints_checked == 1
    assert report.inclusions_checked == 1
    assert report.bad_inclusions == []
    assert report.bad_epoch_signatures == []
    assert any("tenant signature NOT checked" in n for n in report.notes)


def test_verify_bundle_rejects_tampered_entry_payload(tmp_path):
    bundle = _build_ledger_bundle(tmp_path, n_events=2)

    bundle["entries"][0]["event"]["payload"] = {"tampered": True}

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.broken_links


def test_verify_bundle_rejects_checkpoint_whose_epoch_is_missing(tmp_path):
    bundle = _build_ledger_bundle(tmp_path)

    bundle["epochs"] = []  # epoch referenced by the checkpoint is absent

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.bad_inclusions


def test_verify_bundle_reports_malformed_epoch_instead_of_crashing(tmp_path):
    bundle = _build_ledger_bundle(tmp_path)

    # A hand-tampered or partially-written bundle where the epoch entry is
    # missing its root — verify_bundle must report this as a finding, not
    # raise an uncaught exception, since it exists to validate untrusted
    # input.
    del bundle["epochs"][0]["root"]

    report = verify_bundle(bundle)
    assert not report.ok
    assert any("epoch entry skipped" in n for n in report.malformed_bundle)


def test_verify_bundle_accepts_a_valid_tenant_epoch_signature(tmp_path):
    tenant_key = DeviceKey(Ed25519PrivateKey.generate())
    bundle = _build_ledger_bundle(tmp_path, tenant_key=tenant_key)

    report = verify_bundle(bundle)
    assert report.ok, report.notes
    assert report.epoch_signatures_checked == 1
    assert report.bad_epoch_signatures == []
    assert not any("tenant signature NOT checked" in n for n in report.notes)


def test_verify_bundle_rejects_tampered_tenant_epoch_signature(tmp_path):
    tenant_key = DeviceKey(Ed25519PrivateKey.generate())
    bundle = _build_ledger_bundle(tmp_path, tenant_key=tenant_key)

    # The root itself is untouched — only the signed epoch metadata changed
    # after signing, which the tenant signature must catch.
    bundle["epochs"][0]["tenant_id"] = "ten_intruder"

    report = verify_bundle(bundle)
    assert not report.ok
    assert bundle["epochs"][0]["epoch_index"] in report.bad_epoch_signatures


def _minimal_bundle_with_anchor(anchor: dict) -> dict:
    return {
        "bundle_version": "1.0",
        "device": {"device_id": "dev_test", "public_key_b64": None, "key_history": []},
        "entries": [],
        "checkpoints": [],
        "epochs": [
            {
                "epoch_index": 0,
                "root": _RFC3161_FIXTURE_EPOCH_ROOT.hex(),
                "epoch_start": "2026-08-11T00:00:00.000000Z",
                "epoch_end": "2026-08-11T00:10:00.000000Z",
                "tenant_id": "ten_test",
                "anchor": anchor,
            }
        ],
        "range": {"seq_start": None},
    }


def test_verify_bundle_accepts_a_valid_rfc3161_anchor():
    tsr = (_RFC3161_FIXTURE_DIR / "resp.tsr").read_bytes()
    cert_pem = (_RFC3161_FIXTURE_DIR / "tsa_cert.pem").read_bytes()
    cert_der = x509.load_pem_x509_certificate(cert_pem).public_bytes(Encoding.DER)

    bundle = _minimal_bundle_with_anchor(
        {
            "type": "rfc3161_tsa",
            "receipt": {
                "tsr_der_b64": base64.b64encode(tsr).decode(),
                "tsa_certificate_chain_b64": [base64.b64encode(cert_der).decode()],
            },
        }
    )

    report = verify_bundle(bundle)
    assert report.anchors_checked == 1
    assert report.bad_anchors == []
    assert any("no root CA store" in n for n in report.notes)


def test_verify_bundle_rejects_an_rfc3161_anchor_whose_epoch_root_was_swapped():
    tsr = (_RFC3161_FIXTURE_DIR / "resp.tsr").read_bytes()
    cert_pem = (_RFC3161_FIXTURE_DIR / "tsa_cert.pem").read_bytes()
    cert_der = x509.load_pem_x509_certificate(cert_pem).public_bytes(Encoding.DER)

    bundle = _minimal_bundle_with_anchor(
        {
            "type": "rfc3161_tsa",
            "receipt": {
                "tsr_der_b64": base64.b64encode(tsr).decode(),
                "tsa_certificate_chain_b64": [base64.b64encode(cert_der).decode()],
            },
        }
    )
    # Substitute a different epoch root after the receipt was produced — the
    # timestamp's message imprint no longer matches.
    bundle["epochs"][0]["root"] = hashlib.sha256(b"a different epoch root").hexdigest()

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.bad_anchors == [0]


def test_verify_bundle_notes_unanchored_epochs_as_legitimate():
    bundle = _minimal_bundle_with_anchor({"type": "none", "receipt": None})

    report = verify_bundle(bundle)
    assert report.anchors_checked == 0
    assert report.bad_anchors == []
    # anchor.type == "none" is legitimately unanchored, not silently treated
    # as passing — an explicit note says so (§6.2's anchor object; fixes the
    # docstring/code mismatch where BundleVerifyReport's docstring already
    # claimed this but the code produced no note at all).
    assert any("no anchor" in n for n in report.notes)


def test_verify_bundle_rejects_a_device_key_that_does_not_match_the_pin(tmp_path):
    bundle = _build_ledger_bundle(tmp_path)

    report = verify_bundle(
        bundle, pinned_device_public_key_b64="not-the-real-key-base64"
    )
    assert not report.ok
    assert report.device_key_mismatch is True


def test_verify_bundle_accepts_a_device_key_that_matches_the_pin(tmp_path):
    bundle = _build_ledger_bundle(tmp_path)

    report = verify_bundle(
        bundle, pinned_device_public_key_b64=bundle["device"]["public_key_b64"]
    )
    assert report.ok, report.notes
    assert report.device_key_mismatch is False


def test_verify_bundle_rejects_a_tenant_key_not_in_the_pinned_set(tmp_path):
    tenant_key = DeviceKey(Ed25519PrivateKey.generate())
    bundle = _build_ledger_bundle(tmp_path, tenant_key=tenant_key)

    report = verify_bundle(
        bundle,
        pinned_tenant_public_keys_b64=["some-other-tenant-key-base64"],
    )
    assert not report.ok
    assert report.tenant_key_mismatches == [0]


def test_verify_bundle_accepts_a_tenant_key_in_the_pinned_set(tmp_path):
    tenant_key = DeviceKey(Ed25519PrivateKey.generate())
    bundle = _build_ledger_bundle(tmp_path, tenant_key=tenant_key)

    report = verify_bundle(
        bundle, pinned_tenant_public_keys_b64=[tenant_key.public_key_b64]
    )
    assert report.ok, report.notes
    assert report.tenant_key_mismatches == []


def test_verify_bundle_require_pinned_anchors_fails_an_unpinned_rekor_anchor():
    leaf_hash = hashlib.sha256(b"\x00" + _RFC3161_FIXTURE_EPOCH_ROOT).hexdigest()
    bundle = _minimal_bundle_with_anchor(
        {
            "type": "sigstore_rekor",
            "receipt": {
                "log_id": "test",
                "inclusion_proof": {
                    "log_index": 0,
                    "tree_size": 1,
                    "root_hash": leaf_hash,
                    "hashes": [],
                },
                "signed_entry_timestamp": None,
            },
        }
    )
    # Without require_pinned_anchors, an anchor checked only against its own
    # claims (no rekor_public_key_b64) still passes — spec §14 item 4.
    lenient = verify_bundle(bundle)
    assert lenient.bad_anchors == []

    strict = verify_bundle(bundle, require_pinned_anchors=True)
    assert strict.bad_anchors == [0]
    assert not strict.ok


def test_verify_bundle_malformed_checkpoint_entry_is_a_finding_not_a_crash():
    bundle = {
        "bundle_version": "1.0",
        "device": {"device_id": "dev_test", "public_key_b64": None, "key_history": []},
        "entries": [],
        # Missing "checkpoint" object entirely.
        "checkpoints": [{"epoch_index": 0, "inclusion_proof": None}],
        "epochs": [],
        "range": {"seq_start": None},
    }
    report = verify_bundle(bundle)
    assert not report.ok
    assert any("malformed_bundle" in n for n in report.malformed_bundle)


def test_verify_bundle_malformed_inclusion_proof_steps_is_a_finding_not_a_crash(tmp_path):
    bundle = _build_ledger_bundle(tmp_path)
    bundle["checkpoints"][0]["inclusion_proof"]["steps"] = [
        {"sibling": "not-valid-hex", "side": "left"}
    ]

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.malformed_bundle


def test_verify_bundle_missing_epoch_index_is_a_finding_not_a_crash(tmp_path):
    bundle = _build_ledger_bundle(tmp_path, tenant_key=DeviceKey(Ed25519PrivateKey.generate()))
    del bundle["epochs"][0]["epoch_index"]

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.malformed_bundle


def test_verify_bundle_wrong_types_in_entries_is_a_finding_not_a_crash():
    bundle = {
        "bundle_version": "1.0",
        "device": {"device_id": "dev_test", "public_key_b64": None, "key_history": []},
        "entries": ["not an object", 42, {"seq": "not-an-int"}],
        "checkpoints": [],
        "epochs": [],
        "range": {"seq_start": None},
    }
    report = verify_bundle(bundle)
    assert not report.ok
    assert report.malformed_bundle
    assert report.entries_checked == 3


def test_verify_bundle_skips_anchor_verification_when_check_anchors_is_false():
    tsr = (_RFC3161_FIXTURE_DIR / "resp.tsr").read_bytes()
    cert_pem = (_RFC3161_FIXTURE_DIR / "tsa_cert.pem").read_bytes()
    cert_der = x509.load_pem_x509_certificate(cert_pem).public_bytes(Encoding.DER)

    bundle = _minimal_bundle_with_anchor(
        {
            "type": "rfc3161_tsa",
            "receipt": {
                "tsr_der_b64": base64.b64encode(tsr).decode(),
                "tsa_certificate_chain_b64": [base64.b64encode(cert_der).decode()],
            },
        }
    )

    report = verify_bundle(bundle, check_anchors=False)
    assert report.anchors_checked == 0
    assert any("check_anchors=False" in n for n in report.notes)


# --- §14 pinning hardening: pinning now requires something signed --------


def test_verify_bundle_pinned_device_key_with_zero_checkpoints_is_a_finding(tmp_path):
    bundle = _build_ledger_bundle(tmp_path)
    bundle["checkpoints"] = []

    report = verify_bundle(
        bundle, pinned_device_public_key_b64=bundle["device"]["public_key_b64"]
    )
    assert not report.ok
    assert any("zero checkpoints" in n for n in report.malformed_bundle)


def test_verify_bundle_pinned_device_key_flags_an_unsigned_tail(tmp_path):
    """Entries with seq beyond the last checkpoint's seq_end are never
    covered by any signed checkpoint — a caller who pinned the device key
    is asking for full coverage, so this must be a finding, not silence."""
    mock = MockCoriqo()
    ledger, key, device_id = enroll_device(tmp_path, mock)
    try:
        emit_checkpoint(ledger, key, n_events=1)
        shipper = Shipper(
            ledger, key, coriqo_base_url=CORIQO_BASE_URL, http_client=_client(mock)
        )
        try:
            shipper.ship_checkpoints_once()
        finally:
            shipper.close()
        epoch = mock.build_epoch()
        assert epoch is not None
        bundle = _build_bundle(ledger, key.public_key_b64, mock, epoch)

        # Append more entries to the ledger AFTER the checkpoint was taken,
        # and fold them into the bundle's own entries list — they will
        # never be covered by any checkpoint in this bundle.
        from tests.recorder.conftest import make_event

        tail_event = make_event(device_id, session_id="ses_tail")
        tail_entry = ledger.append(tail_event)
        bundle["entries"].append(
            {
                "seq": tail_entry.seq,
                "prev_hash": tail_entry.prev_hash,
                "entry_hash": tail_entry.entry_hash,
                "event": tail_entry.event.to_dict(),
            }
        )
    finally:
        ledger.close()

    report = verify_bundle(bundle, pinned_device_public_key_b64=key.public_key_b64)
    assert not report.ok
    assert report.unsigned_tail == [(tail_entry.seq, tail_entry.seq)]
    assert any("unsigned_tail" in n for n in report.malformed_bundle)


def test_verify_bundle_pinned_tenant_keys_flags_a_missing_tenant_sig(tmp_path):
    bundle = _build_ledger_bundle(tmp_path)  # no tenant_key -> epoch carries no tenant_sig

    report = verify_bundle(
        bundle, pinned_tenant_public_keys_b64=["some-tenant-key-base64"]
    )
    assert not report.ok
    assert bundle["epochs"][0]["epoch_index"] in report.bad_epoch_signatures
    assert any("no tenant_sig at all" in n for n in report.notes)


def test_verify_bundle_pinned_tenant_keys_flags_a_checkpoint_with_no_inclusion_proof(
    tmp_path,
):
    tenant_key = DeviceKey(Ed25519PrivateKey.generate())
    bundle = _build_ledger_bundle(tmp_path, tenant_key=tenant_key)
    # Strip the inclusion proof off the (otherwise valid) checkpoint.
    bundle["checkpoints"][0]["inclusion_proof"] = None
    bundle["checkpoints"][0]["epoch_index"] = None

    report = verify_bundle(
        bundle, pinned_tenant_public_keys_b64=[tenant_key.public_key_b64]
    )
    assert not report.ok
    assert report.bad_inclusions
    assert any("no inclusion proof/epoch_index" in n for n in report.notes)


def test_verify_bundle_require_pinned_anchors_flags_anchor_type_none():
    bundle = _minimal_bundle_with_anchor({"type": "none", "receipt": None})

    report = verify_bundle(bundle, require_pinned_anchors=True)
    assert not report.ok
    assert report.bad_anchors == [0]


def test_verify_bundle_require_pinned_anchors_flags_check_anchors_false():
    tsr = (_RFC3161_FIXTURE_DIR / "resp.tsr").read_bytes()
    cert_pem = (_RFC3161_FIXTURE_DIR / "tsa_cert.pem").read_bytes()
    cert_der = x509.load_pem_x509_certificate(cert_pem).public_bytes(Encoding.DER)

    bundle = _minimal_bundle_with_anchor(
        {
            "type": "rfc3161_tsa",
            "receipt": {
                "tsr_der_b64": base64.b64encode(tsr).decode(),
                "tsa_certificate_chain_b64": [base64.b64encode(cert_der).decode()],
            },
        }
    )

    report = verify_bundle(bundle, check_anchors=False, require_pinned_anchors=True)
    assert not report.ok
    assert report.bad_anchors == [0]


# --- verify_bundle must be total — never raise --------------------------


def test_verify_bundle_never_raises_on_any_single_field_corruption(tmp_path):
    """For a valid bundle, replace each top-level/entry/checkpoint/epoch
    field — including nested fields inside the entry's event, the
    checkpoint wrapper's inner checkpoint object, its inclusion proof, the
    epoch's anchor object, and a key_rotated event's rotation payload — in
    turn with None, "x", [], {}, True and 1.5.

    verify_bundle must never raise for any of these. Beyond that: a field
    that is hashed, signed, or used for inclusion/anchoring must make
    ``ok`` False when corrupted (a bad value there is a real finding, not
    something safe to silently ignore). Only a small, explicit allowlist of
    purely informational/descriptive fields may leave ``ok`` True.
    """
    import copy

    tenant_key = DeviceKey(Ed25519PrivateKey.generate())
    base_bundle = _build_ledger_bundle(tmp_path, tenant_key=tenant_key)
    assert verify_bundle(base_bundle).ok  # sanity: baseline is valid

    # Also cover a key_rotated event's payload fields, and a rekor anchor's
    # receipt fields, which the base ledger bundle above doesn't carry.
    rotation_payload = {
        "old_device_id": "dev_old",
        "new_device_id": "dev_new",
        "new_public_key": "not-checked-here",
        "reason": "scheduled",
        "effective_epoch": 1,
        "cross_signature": "ed25519:not-checked-here",
    }
    rotation_bundle = copy.deepcopy(base_bundle)
    rotation_bundle["entries"][0]["event"]["kind"] = "key_rotated"
    rotation_bundle["entries"][0]["event"]["payload"] = rotation_payload

    rekor_bundle = copy.deepcopy(base_bundle)
    rekor_receipt = {
        "log_index": 0,
        "tree_size": 1,
        "root_hash": hashlib.sha256(b"placeholder-root").hexdigest(),
        "hashes": [],
        "signed_entry_timestamp": None,
    }
    rekor_bundle["epochs"][0]["anchor"] = {"type": "sigstore_rekor", "receipt": rekor_receipt}

    replacements = [None, "x", [], {}, True, 1.5]

    # Fields that are purely descriptive/informational and never feed a
    # hash, signature or inclusion/anchoring check, so corrupting them is
    # allowed to leave ok True. Everything else that gets mutated below is
    # assumed load-bearing unless added here.
    INFORMATIONAL_TOP_LEVEL = {"bundle_version", "range"}
    INFORMATIONAL_DEVICE = {"key_history"}
    INFORMATIONAL_EVENT = {
        # Descriptive/attribution metadata not covered by entry_hash's own
        # chain-link check in a way that changes the verdict on its own —
        # covered instead by the dedicated rotation/session/device tests.
        "model",
        "provider",
        "trace_id",
        "span_id",
        "parent_span_id",
        "continues_from",
        "ts_monotonic_ns",
        "payload_hash",
    }
    # A checkpoint not yet anchored to an epoch (no inclusion_proof/
    # epoch_index at all) is documented as "not yet anchored", not a
    # failure — verify_bundle only turns that into a finding when the
    # caller also passes pinned_tenant_public_keys_b64 (see
    # test_verify_bundle_pinned_tenant_keys_flags_a_checkpoint_with_no_inclusion_proof).
    # This test calls verify_bundle() unpinned, so these stay informational
    # here on purpose.
    INFORMATIONAL_CHECKPOINT_WRAPPER = {"epoch_index", "inclusion_proof"}
    INFORMATIONAL_INNER_CHECKPOINT = {"ts_device"}
    # tenant_kms_public_key_b64/tenant_sig missing or malformed leaves the
    # tenant epoch signature "not checked" rather than failed when
    # unpinned — the same "unchecked, not failed" convention used
    # throughout this module for optional signature checks without a
    # supplied key (mirrors checkpoint pubkey-less verification).
    INFORMATIONAL_EPOCH = {"epoch_start", "epoch_end", "tenant_kms_public_key_b64", "tenant_sig"}
    INFORMATIONAL_ANCHOR: set[str] = set()
    INFORMATIONAL_ROTATION_PAYLOAD = {"reason", "effective_epoch"}
    INFORMATIONAL_REKOR_RECEIPT = {"signed_entry_timestamp"}  # only checked if a pinned key is given
    # A single-checkpoint bundle's inclusion proof has zero Merkle steps
    # (the checkpoint IS the epoch root), so verify_inclusion() never
    # actually consumes leaf_index or a non-empty steps list for it — this
    # is a real, documented limitation of a one-leaf tree, not something
    # this pass is fixing. leaf_index is covered against a real audit path
    # elsewhere (test_epoch_merkle.py, multi-checkpoint trees).
    INFORMATIONAL_PROOF = {"leaf_index"}
    # Labels that are individually known-legitimate exceptions rather than
    # whole-field allowlists (the same field name fails ok=False for other
    # replacement values, so it can't be blanket-allowlisted).
    KNOWN_LEGITIMATE_LABELS = {
        # Unpinned calls only prove internal consistency (spec §10.3) — a
        # missing/malformed device object with no pinned_device_public_key_b64
        # just means nothing is checked against it, not a failure.
        "top-level 'device' -> None",
        "top-level 'device' -> 'x'",
        "top-level 'device' -> []",
        "top-level 'device' -> {}",
        "top-level 'device' -> True",
        "top-level 'device' -> 1.5",
        "device['device_id'] -> None",
        "device['device_id'] -> 'x'",
        "device['device_id'] -> []",
        "device['device_id'] -> {}",
        "device['device_id'] -> True",
        "device['device_id'] -> 1.5",
        "device['public_key_b64'] -> None",
        # Zero checkpoints is documented as legitimate/unchecked unless the
        # caller pins the device key (see
        # test_verify_bundle_pinned_device_key_with_zero_checkpoints_is_a_finding).
        "top-level 'checkpoints' -> []",
        # Steps already == [] in this single-leaf bundle (see
        # INFORMATIONAL_PROOF above) — replacing [] with [] or {} (which
        # iterates to no elements) is not actually a corruption here.
        "checkpoints[0][inclusion_proof]['steps'] -> []",
        "checkpoints[0][inclusion_proof]['steps'] -> {}",
        # anchor.type == 'none' (no anchor at all) is a legitimate,
        # documented bundle state, not a failure — only a present-but-wrong
        # -shaped anchor (checked elsewhere, e.g. 'x') is a finding.
        "epochs[0]['anchor'] -> None",
        "epochs[0]['anchor'] -> {}",
    }

    def _mutations(bundle, allowlists):
        """Yield (label, mutated_bundle, informational) for every top-level
        and nested field this test cares about."""
        for key in list(bundle.keys()):
            informational = key in INFORMATIONAL_TOP_LEVEL
            for rep in replacements:
                mutated = copy.deepcopy(bundle)
                mutated[key] = rep
                yield f"top-level {key!r} -> {rep!r}", mutated, informational

        device = bundle.get("device")
        if isinstance(device, dict):
            for field_name in list(device.keys()):
                informational = field_name in INFORMATIONAL_DEVICE
                for rep in replacements:
                    mutated = copy.deepcopy(bundle)
                    mutated["device"][field_name] = rep
                    yield f"device[{field_name!r}] -> {rep!r}", mutated, informational

        entries = bundle.get("entries")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            for field_name in list(entries[0].keys()):
                informational = False
                for rep in replacements:
                    mutated = copy.deepcopy(bundle)
                    mutated["entries"][0][field_name] = rep
                    yield f"entries[0][{field_name!r}] -> {rep!r}", mutated, informational
            event = entries[0].get("event")
            if isinstance(event, dict):
                for field_name in list(event.keys()):
                    informational = field_name in INFORMATIONAL_EVENT
                    for rep in replacements:
                        mutated = copy.deepcopy(bundle)
                        mutated["entries"][0]["event"][field_name] = rep
                        yield (
                            f"entries[0][event][{field_name!r}] -> {rep!r}",
                            mutated,
                            informational,
                        )
                payload = event.get("payload")
                if isinstance(payload, dict):
                    for field_name in list(payload.keys()):
                        informational = field_name in allowlists.get(
                            "rotation_payload", INFORMATIONAL_ROTATION_PAYLOAD
                        )
                        for rep in replacements:
                            mutated = copy.deepcopy(bundle)
                            mutated["entries"][0]["event"]["payload"][field_name] = rep
                            yield (
                                f"entries[0][event][payload][{field_name!r}] -> {rep!r}",
                                mutated,
                                informational,
                            )

        checkpoints = bundle.get("checkpoints")
        if isinstance(checkpoints, list) and checkpoints and isinstance(checkpoints[0], dict):
            wrapper = checkpoints[0]
            for field_name in list(wrapper.keys()):
                informational = field_name in INFORMATIONAL_CHECKPOINT_WRAPPER
                for rep in replacements:
                    mutated = copy.deepcopy(bundle)
                    mutated["checkpoints"][0][field_name] = rep
                    yield f"checkpoints[0][{field_name!r}] -> {rep!r}", mutated, informational
            inner = wrapper.get("checkpoint")
            if isinstance(inner, dict):
                for field_name in list(inner.keys()):
                    informational = field_name in INFORMATIONAL_INNER_CHECKPOINT
                    for rep in replacements:
                        mutated = copy.deepcopy(bundle)
                        mutated["checkpoints"][0]["checkpoint"][field_name] = rep
                        yield (
                            f"checkpoints[0][checkpoint][{field_name!r}] -> {rep!r}",
                            mutated,
                            informational,
                        )
            proof = wrapper.get("inclusion_proof")
            if isinstance(proof, dict):
                for field_name in list(proof.keys()):
                    informational = field_name in INFORMATIONAL_PROOF
                    for rep in replacements:
                        mutated = copy.deepcopy(bundle)
                        mutated["checkpoints"][0]["inclusion_proof"][field_name] = rep
                        yield (
                            f"checkpoints[0][inclusion_proof][{field_name!r}] -> {rep!r}",
                            mutated,
                            informational,
                        )

        epochs = bundle.get("epochs")
        if isinstance(epochs, list) and epochs and isinstance(epochs[0], dict):
            epoch0 = epochs[0]
            for field_name in list(epoch0.keys()):
                informational = field_name in INFORMATIONAL_EPOCH
                for rep in replacements:
                    mutated = copy.deepcopy(bundle)
                    mutated["epochs"][0][field_name] = rep
                    yield f"epochs[0][{field_name!r}] -> {rep!r}", mutated, informational
            anchor = epoch0.get("anchor")
            if isinstance(anchor, dict):
                for field_name in list(anchor.keys()):
                    # Same reasoning as the receipt sub-loop below: when
                    # anchor.type == "none", the receipt field is never
                    # looked at at all, so mutating it can't surface a
                    # finding — that's not a gap, it's the field being
                    # genuinely inert for this anchor type.
                    if field_name == "receipt" and anchor.get("type") == "none":
                        continue
                    informational = field_name in INFORMATIONAL_ANCHOR
                    for rep in replacements:
                        mutated = copy.deepcopy(bundle)
                        mutated["epochs"][0]["anchor"][field_name] = rep
                        yield (
                            f"epochs[0][anchor][{field_name!r}] -> {rep!r}",
                            mutated,
                            informational,
                        )
                receipt = anchor.get("receipt")
                # anchor.type == "none" short-circuits before the receipt is
                # ever looked at, so mutating it there is a no-op, not a
                # meaningful check — only fuzz it on a bundle with a real
                # anchor type (rekor_bundle, below).
                if isinstance(receipt, dict) and anchor.get("type") != "none":
                    for field_name in list(receipt.keys()):
                        informational = field_name in allowlists.get(
                            "rekor_receipt", INFORMATIONAL_REKOR_RECEIPT
                        )
                        for rep in replacements:
                            mutated = copy.deepcopy(bundle)
                            mutated["epochs"][0]["anchor"]["receipt"][field_name] = rep
                            yield (
                                f"epochs[0][anchor][receipt][{field_name!r}] -> {rep!r}",
                                mutated,
                                informational,
                            )

    failures: list[str] = []
    for bundle, allowlists in (
        (base_bundle, {}),
        (rotation_bundle, {"rotation_payload": INFORMATIONAL_ROTATION_PAYLOAD}),
        (rekor_bundle, {"rekor_receipt": INFORMATIONAL_REKOR_RECEIPT}),
    ):
        for label, mutated, informational in _mutations(bundle, allowlists):
            try:
                report = verify_bundle(mutated)
            except Exception as exc:  # pragma: no cover - the thing under test
                raise AssertionError(f"verify_bundle raised for {label}: {exc!r}") from exc
            assert report is not None, label
            if not informational and label not in KNOWN_LEGITIMATE_LABELS and report.ok:
                failures.append(label)

    assert not failures, (
        "verify_bundle left ok=True for a load-bearing field corruption "
        f"(should have been a finding): {failures}"
    )


# --- named regressions for the malformed-input hardening pass ------------
#
# Each of these reproduces one exact mutation from the lead's repro_bundle.py
# (cases D-N) that previously either raised an uncaught exception or left
# ok=True for a field that matters. verify_bundle must never raise, and must
# report ok is False with a finding for each of these.


def test_verify_bundle_key_rotated_payload_that_is_a_string_is_a_finding(tmp_path):
    """D: a key_rotated event's payload is a bare string, not an object —
    _check_rotation{,_live} call .get() on it, which used to raise
    AttributeError."""
    bundle = _build_ledger_bundle(tmp_path)
    bundle["entries"][0]["event"]["kind"] = "key_rotated"
    bundle["entries"][0]["event"]["payload"] = "zzz"

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.malformed_bundle


def test_verify_bundle_epoch_anchor_that_is_a_string_is_a_finding(tmp_path):
    """E: an epoch's anchor field is a bare string instead of an object —
    this used to be silently coerced to {} (anchor.type=='none'), passing as
    a legitimately unanchored epoch instead of a malformed one."""
    bundle = _build_ledger_bundle(tmp_path)
    bundle["epochs"][0]["anchor"] = "rfc3161_tsa"

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.malformed_bundle


def test_verify_bundle_checkpoint_seq_end_string_is_a_finding_not_a_crash(tmp_path):
    """F: a checkpoint's seq_end is a non-numeric string — int("abc") used
    to raise ValueError out of _verify_checkpoints."""
    bundle = _build_ledger_bundle(tmp_path)
    bundle["checkpoints"][0]["checkpoint"]["seq_end"] = "abc"

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.bad_checkpoint_signatures or report.malformed_bundle


def test_verify_bundle_checkpoint_seq_end_none_is_a_finding_not_a_crash(tmp_path):
    """G: a checkpoint's seq_end is None — int(None) used to raise
    TypeError out of _verify_checkpoints."""
    bundle = _build_ledger_bundle(tmp_path)
    bundle["checkpoints"][0]["checkpoint"]["seq_end"] = None

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.bad_checkpoint_signatures or report.malformed_bundle


def test_verify_bundle_rekor_receipt_that_is_a_string_is_a_finding(tmp_path):
    """I: a sigstore_rekor anchor's receipt is a bare string, not an object
    — verify_rekor_receipt's inclusion.get()/... calls used to raise
    AttributeError via _verify_anchor."""
    bundle = _build_ledger_bundle(tmp_path)
    bundle["epochs"][0]["anchor"] = {"type": "sigstore_rekor", "receipt": "s"}

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.bad_anchors


def test_verify_bundle_epoch_root_bad_hex_with_anchor_none_is_a_finding(tmp_path):
    """K: an extra epoch entry has a non-hex root and anchor.type == 'none'
    — this used to be swallowed as a note on the (unused) epoch_roots build
    and never surfaced as a finding at all."""
    bundle = _build_ledger_bundle(tmp_path)
    bundle["epochs"].append({"epoch_index": 99, "root": "zz", "anchor": {"type": "none"}})

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.malformed_bundle


def test_verify_bundle_is_a_list_returns_a_report_not_a_raise():
    """M: the bundle itself is the wrong shape entirely (a list, not an
    object) — bundle.get("device") used to raise AttributeError before any
    validation could even start."""
    report = verify_bundle([])
    assert report.ok is False
    assert report.malformed_bundle


def test_verify_bundle_entry_seq_that_is_a_bool_is_a_finding(tmp_path):
    """N: an entry's seq is True (a bool). bool is an int subclass in
    Python, so int(True) == 1 used to be silently accepted as seq 1 —
    seq must be a genuine int, never a bool/str/float standing in for one."""
    bundle = _build_ledger_bundle(tmp_path)
    bundle["entries"][0]["seq"] = True

    report = verify_bundle(bundle)
    assert not report.ok
    assert report.malformed_bundle


def test_verify_bundle_checkpoint_with_nan_field_is_a_finding_not_a_raise(tmp_path):
    """A checkpoint field that is NaN (or another value Python's json module
    accepts but strict JSON/canonical.py does not, e.g. a lone UTF-16
    surrogate) used to blow straight through _verify_checkpoints:
    canonicalize(unsigned) raised CanonicalizationError out of verify_bundle
    entirely, rather than being treated as that one checkpoint's signature
    check failing. verify_bundle must be total over untrusted checkpoint
    content, exactly like it already is for entries."""
    bundle = _build_ledger_bundle(tmp_path)
    bundle["checkpoints"][0]["checkpoint"]["seq_start"] = float("nan")

    report = verify_bundle(bundle)  # must not raise
    assert report.ok is False
    assert report.bad_checkpoint_signatures


def test_verify_bundle_checkpoint_with_lone_surrogate_is_a_finding_not_a_raise(tmp_path):
    """Same as the NaN case above, but for a lone UTF-16 surrogate
    (\\ud800) — also accepted by json.loads but rejected by
    canonical.py's stricter canonicalize()."""
    bundle = _build_ledger_bundle(tmp_path)
    bundle["checkpoints"][0]["checkpoint"]["ts_device"] = "\ud800"

    report = verify_bundle(bundle)  # must not raise
    assert report.ok is False
    assert report.bad_checkpoint_signatures

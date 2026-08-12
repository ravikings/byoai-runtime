"""End-to-end test of verify_bundle() against a bundle assembled from a real
Ledger + Checkpointer + MockCoriqo epoch — steps 1-3 of the examiner export
bundle verification path sketched in
``internal_doc/recorder_contract_export_bundle.md``.

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
    assert any("malformed epoch entry" in n for n in report.notes)


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
    assert not any("anchor" in n for n in report.notes)


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

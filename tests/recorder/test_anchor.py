"""Tests for level-4 external anchor receipt verification (anchor.py).

RFC 3161 fixtures under ``tests/recorder/fixtures/rfc3161/`` are a real
timestamp response generated once via ``openssl ts -reply`` against a
throwaway self-signed TSA cert, over the SHA-256 digest of the literal bytes
``b"fake epoch root"`` — checked in rather than regenerated per test run so
these tests don't depend on a system ``openssl`` binary being present.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA, SECP256R1, generate_private_key
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import Encoding

from byoai.recorder.anchor import verify_rekor_receipt, verify_rfc3161_receipt
from byoai.recorder.canonical import canonicalize

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "rfc3161"
_FIXTURE_EPOCH_ROOT = hashlib.sha256(b"fake epoch root").digest()


def _rfc3161_receipt() -> dict:
    tsr = (_FIXTURE_DIR / "resp.tsr").read_bytes()
    cert_pem = (_FIXTURE_DIR / "tsa_cert.pem").read_bytes()
    cert_der = x509.load_pem_x509_certificate(cert_pem).public_bytes(Encoding.DER)
    return {
        "tsr_der_b64": base64.b64encode(tsr).decode(),
        "tsa_certificate_chain_b64": [base64.b64encode(cert_der).decode()],
    }


def test_verify_rfc3161_receipt_accepts_a_valid_timestamp():
    ok, notes = verify_rfc3161_receipt(_rfc3161_receipt(), _FIXTURE_EPOCH_ROOT)
    assert ok, notes
    assert any("no root CA store" in n for n in notes)


def test_verify_rfc3161_receipt_rejects_wrong_epoch_root():
    ok, notes = verify_rfc3161_receipt(_rfc3161_receipt(), hashlib.sha256(b"wrong root").digest())
    assert not ok
    assert any("does NOT verify" in n for n in notes)


def test_verify_rfc3161_receipt_rejects_malformed_receipt():
    ok, notes = verify_rfc3161_receipt({"tsr_der_b64": "not base64!!"}, _FIXTURE_EPOCH_ROOT)
    assert not ok
    assert any("malformed" in n for n in notes)


# --- Rekor -------------------------------------------------------------


def _leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _build_rekor_fixture(epoch_root: bytes, index: int, size: int):
    """Build a tiny real RFC 6962 tree over ``size`` leaves and return the
    audit path for ``index``, with leaf ``index`` fixed to ``epoch_root``.
    """
    leaves_data = [f"leaf-{i}".encode() for i in range(size)]
    hashes = [_leaf_hash(d) for d in leaves_data]
    hashes[index] = _leaf_hash(epoch_root)

    def mth(level: list[bytes]) -> bytes:
        n = len(level)
        if n == 1:
            return level[0]
        k = 1
        while k * 2 < n:
            k *= 2
        return _node_hash(mth(level[:k]), mth(level[k:]))

    def path(m: int, level: list[bytes]) -> list[bytes]:
        n = len(level)
        if n == 1:
            return []
        k = 1
        while k * 2 < n:
            k *= 2
        if m < k:
            return path(m, level[:k]) + [mth(level[k:])]
        return path(m - k, level[k:]) + [mth(level[:k])]

    root = mth(hashes)
    audit_path = path(index, hashes)
    return root, audit_path


def test_verify_rekor_receipt_accepts_a_valid_inclusion_proof():
    epoch_root = hashlib.sha256(b"some tenant epoch root").digest()
    root, audit_path = _build_rekor_fixture(epoch_root, index=3, size=7)

    receipt = {
        "log_index": 3,
        "tree_size": 7,
        "root_hash": root.hex(),
        "hashes": [h.hex() for h in audit_path],
    }

    ok, notes = verify_rekor_receipt(receipt, epoch_root, rekor_public_key_b64=None)
    assert ok, notes
    assert any("NOT checked" in n for n in notes)


def test_verify_rekor_receipt_rejects_tampered_root():
    epoch_root = hashlib.sha256(b"some tenant epoch root").digest()
    root, audit_path = _build_rekor_fixture(epoch_root, index=3, size=7)

    receipt = {
        "log_index": 3,
        "tree_size": 7,
        "root_hash": hashlib.sha256(b"wrong root").hexdigest(),
        "hashes": [h.hex() for h in audit_path],
    }

    ok, notes = verify_rekor_receipt(receipt, epoch_root, rekor_public_key_b64=None)
    assert not ok
    assert any("does NOT reconstruct" in n for n in notes)


def test_verify_rekor_receipt_checks_signed_entry_timestamp():
    epoch_root = hashlib.sha256(b"another tenant epoch root").digest()
    root, audit_path = _build_rekor_fixture(epoch_root, index=1, size=4)

    rekor_key = generate_private_key(SECP256R1())

    signed_fields = {"log_index": 1, "tree_size": 4, "root_hash": root.hex()}
    signature = rekor_key.sign(canonicalize(signed_fields), ECDSA(SHA256()))

    receipt = {
        "log_index": 1,
        "tree_size": 4,
        "root_hash": root.hex(),
        "hashes": [h.hex() for h in audit_path],
        "signed_entry_timestamp": base64.b64encode(signature).decode(),
    }

    # This receipt's SET is ECDSA/P-256, not the recorder's Ed25519
    # DeviceKey.verify — the point here is just that a *missing* SET is
    # honestly reported and a present one gets exercised, not that this
    # exact scheme is what a real Rekor deployment uses.
    ok, notes = verify_rekor_receipt(receipt, epoch_root, rekor_public_key_b64="not-an-ed25519-key")
    assert not ok
    assert any("does NOT verify" in n for n in notes)


def test_verify_rekor_receipt_rejects_negative_log_index():
    epoch_root = hashlib.sha256(b"yet another tenant epoch root").digest()
    _, audit_path = _build_rekor_fixture(epoch_root, index=3, size=7)

    receipt = {
        "log_index": -1,
        "tree_size": 7,
        "root_hash": hashlib.sha256(b"whatever").hexdigest(),
        "hashes": [h.hex() for h in audit_path],
    }

    ok, notes = verify_rekor_receipt(receipt, epoch_root, rekor_public_key_b64=None)
    assert not ok
    assert any("out of range" in n for n in notes)


def test_verify_rekor_receipt_rejects_log_index_at_or_above_tree_size():
    epoch_root = hashlib.sha256(b"yet another tenant epoch root 2").digest()
    _, audit_path = _build_rekor_fixture(epoch_root, index=3, size=7)

    receipt = {
        "log_index": 7,
        "tree_size": 7,
        "root_hash": hashlib.sha256(b"whatever").hexdigest(),
        "hashes": [h.hex() for h in audit_path],
    }

    ok, notes = verify_rekor_receipt(receipt, epoch_root, rekor_public_key_b64=None)
    assert not ok
    assert any("out of range" in n for n in notes)


def test_verify_rekor_receipt_rejects_pathologically_long_audit_path():
    """A malicious receipt can pad ``hashes`` far beyond what any valid proof
    for its claimed ``tree_size`` could contain. The old code relied only on
    a ``try/except ValueError`` around a recursive reconstruction, so a long
    enough list (crafted so the ``index < k`` branch is always taken, e.g.
    ``log_index = 0``) would blow past Python's recursion limit and raise an
    uncaught ``RecursionError`` instead of returning a clean failure. This
    must now be rejected cheaply, before any hashing happens, and must never
    raise.
    """
    epoch_root = hashlib.sha256(b"padded receipt epoch root").digest()
    dummy_hash = hashlib.sha256(b"dummy").hexdigest()

    receipt = {
        "log_index": 0,
        "tree_size": 7,
        "root_hash": hashlib.sha256(b"whatever").hexdigest(),
        "hashes": [dummy_hash] * 5000,
    }

    ok, notes = verify_rekor_receipt(receipt, epoch_root, rekor_public_key_b64=None)
    assert not ok
    assert any("more than" in n for n in notes)


def test_verify_rekor_receipt_reconstruction_never_raises_recursion_error():
    """Even if a pathologically long audit path somehow got past the
    up-front length cap, the reconstruction itself (now iterative) must not
    raise ``RecursionError`` — it should surface as a normal ``(False, [...])``
    result instead. Exercise the recursion-prone helper directly to prove
    the iterative rewrite has no recursion depth at all.
    """
    from byoai.recorder.anchor import _root_from_audit_path

    leaf_hash = hashlib.sha256(b"leaf").digest()
    dummy = hashlib.sha256(b"dummy").digest()
    huge_size = 2**5000  # tree size far beyond any real system
    path = [dummy] * 5000

    # index=0 forces the "left" branch at every level, so a recursive
    # implementation would recurse len(path) times; the iterative
    # implementation must handle this without raising RecursionError.
    try:
        _root_from_audit_path(leaf_hash, 0, huge_size, path)
    except RecursionError:
        raise AssertionError("audit path reconstruction must not recurse")
    except ValueError:
        pass  # fine — shape mismatch is an expected, clean failure

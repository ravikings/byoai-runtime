"""Tests for verify_checkpoint_epoch_inclusion — the offline link between a
device checkpoint and a tenant epoch root (spec section 6.3)."""

from __future__ import annotations

from byoai.recorder.merkle import build_epoch_tree, checkpoint_leaf_hash
from byoai.recorder.verify import verify_checkpoint_epoch_inclusion


def _checkpoints(n: int) -> list[dict]:
    return [
        {"device_id": f"dev_{i}", "seq_start": 0, "seq_end": 10, "sig": f"ed25519:{i}"}
        for i in range(n)
    ]


def test_valid_checkpoint_verifies_against_its_epoch_root():
    checkpoints = _checkpoints(5)
    root, proofs = build_epoch_tree(checkpoints)

    for cp, proof in zip(checkpoints, proofs, strict=True):
        assert verify_checkpoint_epoch_inclusion(cp, proof, root)


def test_rejects_checkpoint_that_does_not_match_the_proofs_leaf():
    checkpoints = _checkpoints(5)
    root, proofs = build_epoch_tree(checkpoints)

    # Swap in a checkpoint that was never part of this tree at all.
    forged = {"device_id": "dev_intruder", "seq_start": 0, "seq_end": 999, "sig": "ed25519:x"}
    assert checkpoint_leaf_hash(forged) != proofs[0].leaf_hash
    assert not verify_checkpoint_epoch_inclusion(forged, proofs[0], root)


def test_rejects_proof_from_a_different_epoch():
    checkpoints = _checkpoints(5)
    _root_a, proofs_a = build_epoch_tree(checkpoints)
    root_b, _proofs_b = build_epoch_tree(_checkpoints(3))

    # A proof that is internally valid against epoch A's root must not
    # verify against epoch B's root just because both are hashes.
    assert not verify_checkpoint_epoch_inclusion(checkpoints[0], proofs_a[0], root_b)


def test_rejects_tampered_checkpoint_even_if_leaf_hash_field_is_faked():
    checkpoints = _checkpoints(4)
    root, proofs = build_epoch_tree(checkpoints)

    tampered = dict(checkpoints[2], sig="ed25519:tampered")
    # The checkpoint itself changed after the proof was issued for the
    # original — re-deriving the leaf hash from the tampered checkpoint
    # must catch this even though proofs[2].leaf_hash still reflects the
    # original, untampered checkpoint.
    assert not verify_checkpoint_epoch_inclusion(tampered, proofs[2], root)


def test_single_checkpoint_epoch_root_is_the_checkpoint_leaf_hash():
    (checkpoint,) = _checkpoints(1)
    root, proofs = build_epoch_tree([checkpoint])
    assert root == checkpoint_leaf_hash(checkpoint)
    assert verify_checkpoint_epoch_inclusion(checkpoint, proofs[0], root)

"""Epoch Merkle tree tests — spec section 6.2 level 3."""

from __future__ import annotations

import hashlib

import pytest

from byoai.recorder.merkle import (
    InclusionProof,
    MerkleTree,
    build_epoch_tree,
    checkpoint_leaf_hash,
    verify_inclusion,
)


def _leaves(n: int) -> list[bytes]:
    return [hashlib.sha256(f"leaf-{i}".encode()).digest() for i in range(n)]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 16, 17])
def test_every_leaf_has_a_valid_inclusion_proof(n: int) -> None:
    tree = MerkleTree(_leaves(n))
    for i in range(n):
        proof = tree.proof(i)
        assert verify_inclusion(proof)
        assert proof.root == tree.root


def test_single_leaf_tree_root_is_the_leaf_hash() -> None:
    (leaf,) = _leaves(1)
    tree = MerkleTree([leaf])
    assert tree.root == leaf
    proof = tree.proof(0)
    assert proof.steps == ()
    assert verify_inclusion(proof)


def test_root_changes_if_any_leaf_changes() -> None:
    a = MerkleTree(_leaves(5))
    tampered = _leaves(5)
    tampered[3] = hashlib.sha256(b"tampered").digest()
    b = MerkleTree(tampered)
    assert a.root != b.root


def test_proof_does_not_verify_against_wrong_root() -> None:
    tree = MerkleTree(_leaves(5))
    other = MerkleTree(_leaves(5)[:-1] + [hashlib.sha256(b"different").digest()])
    proof = tree.proof(2)
    forged = InclusionProof(
        leaf_index=proof.leaf_index,
        leaf_hash=proof.leaf_hash,
        steps=proof.steps,
        root=other.root,
    )
    assert not verify_inclusion(forged)


def test_proof_does_not_verify_for_wrong_leaf() -> None:
    tree = MerkleTree(_leaves(5))
    proof = tree.proof(2)
    wrong_leaf = InclusionProof(
        leaf_index=proof.leaf_index,
        leaf_hash=hashlib.sha256(b"not-the-real-leaf").digest(),
        steps=proof.steps,
        root=proof.root,
    )
    assert not verify_inclusion(wrong_leaf)


def test_odd_node_is_promoted_not_duplicated() -> None:
    # A 3-leaf tree must not be forgeable by treating it as if leaf 2 were
    # duplicated to pad to 4 — assert the actual construction against a
    # hand-computed root.
    leaves = _leaves(3)
    tree = MerkleTree(leaves)

    def node(left: bytes, right: bytes) -> bytes:
        return hashlib.sha256(b"\x01" + left + right).digest()

    expected_level1_0 = node(leaves[0], leaves[1])
    expected_root = node(expected_level1_0, leaves[2])  # leaf 2 promoted unchanged
    assert tree.root == expected_root


def test_tree_rejects_empty_leaves() -> None:
    with pytest.raises(ValueError):
        MerkleTree([])


def test_proof_rejects_out_of_range_index() -> None:
    tree = MerkleTree(_leaves(3))
    with pytest.raises(IndexError):
        tree.proof(3)
    with pytest.raises(IndexError):
        tree.proof(-1)


def test_checkpoint_leaf_hash_is_order_independent_of_key_order() -> None:
    cp_a = {"device_id": "dev_1", "seq_start": 1, "seq_end": 2, "sig": "ed25519:xx"}
    cp_b = {"sig": "ed25519:xx", "seq_end": 2, "device_id": "dev_1", "seq_start": 1}
    assert checkpoint_leaf_hash(cp_a) == checkpoint_leaf_hash(cp_b)


def test_checkpoint_leaf_hash_changes_if_signature_changes() -> None:
    cp_a = {"device_id": "dev_1", "seq_start": 1, "seq_end": 2, "sig": "ed25519:xx"}
    cp_b = {**cp_a, "sig": "ed25519:yy"}
    assert checkpoint_leaf_hash(cp_a) != checkpoint_leaf_hash(cp_b)


def test_build_epoch_tree_returns_a_verifiable_proof_per_checkpoint() -> None:
    checkpoints = [
        {"device_id": f"dev_{i}", "seq_start": 0, "seq_end": 10, "sig": f"ed25519:{i}"}
        for i in range(6)
    ]
    root, proofs = build_epoch_tree(checkpoints)
    assert len(proofs) == len(checkpoints)
    for i, (cp, proof) in enumerate(zip(checkpoints, proofs, strict=True)):
        assert proof.leaf_index == i
        assert proof.leaf_hash == checkpoint_leaf_hash(cp)
        assert proof.root == root
        assert verify_inclusion(proof)


def test_build_epoch_tree_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        build_epoch_tree([])


def test_proof_step_sides_are_consistent_with_verification() -> None:
    # Regression guard: a proof built with steps in the wrong order or wrong
    # side must fail rather than silently verify.
    tree = MerkleTree(_leaves(4))
    proof = tree.proof(1)
    scrambled = InclusionProof(
        leaf_index=proof.leaf_index,
        leaf_hash=proof.leaf_hash,
        steps=tuple(reversed(proof.steps)) if len(proof.steps) > 1 else proof.steps,
        root=proof.root,
    )
    if len(proof.steps) > 1:
        assert not verify_inclusion(scrambled)

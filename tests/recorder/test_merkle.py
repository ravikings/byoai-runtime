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


def _rfc6962_mth(leaf_hashes: list[bytes]) -> bytes:
    """RFC 6962 §2.1 Merkle Tree Hash, written recursively from the RFC text.

    An independent second implementation on purpose: comparing MerkleTree
    against vectors MerkleTree generated would only prove it agrees with
    itself.
    """
    n = len(leaf_hashes)
    if n == 1:
        return leaf_hashes[0]
    k = 1
    while k * 2 < n:
        k *= 2
    return hashlib.sha256(
        b"\x01" + _rfc6962_mth(leaf_hashes[:k]) + _rfc6962_mth(leaf_hashes[k:])
    ).digest()


@pytest.mark.parametrize("n", list(range(1, 130)) + [255, 256, 257, 1000, 2000])
def test_promotion_is_exactly_the_rfc6962_tree(n: int) -> None:
    """Promoting a lone odd node IS RFC 6962's power-of-two split, not an
    approximation of it.

    This also pins cross-repo agreement: Coriqo's checkpoint service builds
    tree_version 2 the same way, so a root computed here and a root computed
    there are the same root over the same leaves.
    """
    leaves = _leaves(n)
    assert MerkleTree(leaves).root == _rfc6962_mth(leaves)


def test_the_duplicating_construction_would_collide() -> None:
    """Why promotion, stated as a test rather than a comment.

    Duplicating the last node makes [a, b, c] and [a, b, c, c] share a root, so
    the root stops identifying its leaf set. Promotion does not.
    """
    a, b, c = _leaves(3)

    def dup_root(ls: list[bytes]) -> bytes:
        cur = list(ls)
        while len(cur) > 1:
            cur = [
                hashlib.sha256(
                    b"\x01" + cur[i] + (cur[i + 1] if i + 1 < len(cur) else cur[i])
                ).digest()
                for i in range(0, len(cur), 2)
            ]
        return cur[0]

    assert dup_root([a, b, c]) == dup_root([a, b, c, c])
    assert MerkleTree([a, b, c]).root != MerkleTree([a, b, c, c]).root

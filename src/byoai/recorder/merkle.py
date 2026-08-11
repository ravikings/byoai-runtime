"""Epoch Merkle trees over device checkpoints (spec section 6.2, level 3).

Coriqo builds one of these per tenant per epoch (10 minutes), over the
checkpoints it received from that tenant's devices in the window. The root
is what gets signed with the tenant KMS key and submitted to an external
anchor (level 4, not implemented here).

This module is deliberately dependency-free and side-effect-free: it takes a
list of checkpoint leaves in and returns a root plus per-leaf inclusion
proofs, so it can run identically inside Coriqo (building the tree) and
inside an offline verifier (checking a proof against a root someone else
published). Domain-separated hashing (leaf vs. internal-node prefixes)
follows RFC 6962 so a leaf hash can never be replayed as an internal node
hash or vice versa.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from byoai.recorder.canonical import canonicalize

__all__ = [
    "MerkleTree",
    "InclusionProof",
    "ProofStep",
    "checkpoint_leaf_hash",
    "build_epoch_tree",
    "verify_inclusion",
]

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"

Side = Literal["left", "right"]


def _leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(_LEAF_PREFIX + data).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


def checkpoint_leaf_hash(checkpoint: dict) -> bytes:
    """The leaf hash a checkpoint contributes to its tenant's epoch tree.

    Hashes the whole checkpoint as received (including ``sig``) — the epoch
    tree attests to *this exact checkpoint document*, signature and all, not
    just the device chain segment it describes.
    """
    return _leaf_hash(canonicalize(checkpoint))


@dataclass(frozen=True, slots=True)
class ProofStep:
    """One level of an inclusion proof: the sibling hash and which side it
    sits on relative to the node being folded in."""

    sibling: bytes
    side: Side


@dataclass(frozen=True, slots=True)
class InclusionProof:
    """Proof that a leaf at ``leaf_index`` is included under ``root``."""

    leaf_index: int
    leaf_hash: bytes
    steps: tuple[ProofStep, ...]
    root: bytes


class MerkleTree:
    """A binary Merkle tree over pre-hashed leaves.

    Odd node counts at any level are handled by promoting the lone leftover
    node unchanged to the next level (RFC 6962 style) rather than duplicating
    it — duplication lets an attacker pad a tree with copies of the last leaf
    to forge inclusion proofs for a leaf that only appears once.
    """

    def __init__(self, leaf_hashes: list[bytes]) -> None:
        if not leaf_hashes:
            raise ValueError("a Merkle tree needs at least one leaf")
        self._levels: list[list[bytes]] = [list(leaf_hashes)]
        level = self._levels[0]
        while len(level) > 1:
            level = _build_next_level(level)
            self._levels.append(level)

    @property
    def root(self) -> bytes:
        return self._levels[-1][0]

    @property
    def leaf_count(self) -> int:
        return len(self._levels[0])

    def proof(self, leaf_index: int) -> InclusionProof:
        if not 0 <= leaf_index < self.leaf_count:
            raise IndexError(f"leaf index {leaf_index} out of range")

        steps: list[ProofStep] = []
        index = leaf_index
        for level in self._levels[:-1]:
            if index % 2 == 0:
                sibling_index = index + 1
                if sibling_index < len(level):
                    steps.append(ProofStep(sibling=level[sibling_index], side="right"))
                # else: index was the odd node promoted unchanged — no step.
            else:
                steps.append(ProofStep(sibling=level[index - 1], side="left"))
            index //= 2

        return InclusionProof(
            leaf_index=leaf_index,
            leaf_hash=self._levels[0][leaf_index],
            steps=tuple(steps),
            root=self.root,
        )


def _build_next_level(level: list[bytes]) -> list[bytes]:
    next_level: list[bytes] = []
    i = 0
    n = len(level)
    while i + 1 < n:
        next_level.append(_node_hash(level[i], level[i + 1]))
        i += 2
    if i < n:
        next_level.append(level[i])  # lone node, promoted unchanged
    return next_level


def verify_inclusion(proof: InclusionProof) -> bool:
    """Independently recompute the root from ``proof`` and compare.

    Takes no tree and no network access — this is the offline half of the
    verification path in spec section 6.3.
    """
    current = proof.leaf_hash
    for step in proof.steps:
        if step.side == "right":
            current = _node_hash(current, step.sibling)
        else:
            current = _node_hash(step.sibling, current)
    return current == proof.root


def build_epoch_tree(checkpoints: list[dict]) -> tuple[bytes, list[InclusionProof]]:
    """Build a tenant epoch tree over ``checkpoints`` (in receipt order).

    Returns the epoch root and one inclusion proof per checkpoint, indexed
    the same way as the input list.
    """
    if not checkpoints:
        raise ValueError("cannot build an epoch tree over zero checkpoints")
    leaves = [checkpoint_leaf_hash(cp) for cp in checkpoints]
    tree = MerkleTree(leaves)
    proofs = [tree.proof(i) for i in range(len(checkpoints))]
    return tree.root, proofs

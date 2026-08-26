"""Shared fixtures and the one device-identity helper for the ingest tests.

``IngestStore.record_enrolment`` now refuses any enrolment whose ``device_id``
is not ``derive_device_id(public_key_b64)``, so tests can no longer invent
identifiers like ``"dev-a"``: an identity here must be a real Ed25519 keypair
with its id derived from the real public key, exactly as a device would
present it. Deriving the private key from a seed keeps ids deterministic
across runs (readable failures, no fixture churn) while still exercising the
real key type, the real base64 encoding and the real derivation.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from byoai.ingest.store import IngestStore
from byoai.recorder.keys import derive_device_id


def device_identity(seed: str) -> tuple[str, str]:
    """Return ``(public_key_b64, device_id)`` for a real Ed25519 keypair.

    The returned pair is the only kind ``record_enrolment`` accepts: the id is
    derived from the key, not chosen alongside it.
    """
    private = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed.encode()).digest())
    public_key_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")
    return public_key_b64, derive_device_id(public_key_b64)


@pytest.fixture()
def store(tmp_path):
    with IngestStore(tmp_path / "ingest.db") as s:
        yield s

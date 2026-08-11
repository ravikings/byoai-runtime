"""Device key rotation and revocation for the agent recorder (spec §8.4).

Rotation replaces a device's Ed25519 keypair while preserving verifiable
continuity of the ledger's hash chain across the key boundary: the OLD key
cross-signs the NEW public key, and that cross-signature is sealed into the
ledger as a ``KEY_ROTATED`` event — the last thing the retiring identity
signs — before the on-disk key files are replaced.

Revocation reuses the same mechanism with ``reason="revocation"`` (or
``"compromise"``): the event's ``effective_epoch`` marks the point after
which entries still attributed to the old device are no longer trusted,
while entries signed before it remain valid (spec §8.4).

Client-side only. Anchoring the revocation event externally (Coriqo) is out
of scope here; it will ship like any other ledger event via the shipper.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .canonical import canonicalize, sha256_hex
from .keys import DeviceKey, load_or_create_device_key
from .ledger import Ledger
from .schema import AgentEvent, EventKind, new_event_id, now_monotonic_ns, now_ts_device

__all__ = ["ROTATION_SESSION_ID", "rotate_key", "rotate_cli"]

# KEY_ROTATED events are device-level, not tied to any particular agent
# session, but AgentEvent.session_id is required — this sentinel makes that
# explicit rather than reusing a real session_id.
ROTATION_SESSION_ID = "_key_rotation"

_VALID_REASONS = frozenset({"rotation", "revocation", "compromise"})


def rotate_key(
    key_dir: Path,
    ledger: Ledger,
    *,
    reason: str = "rotation",
) -> DeviceKey:
    """Rotate this device's Ed25519 key, cross-signed for chain continuity.

    Generates a new keypair, has the CURRENT on-disk key cross-sign the new
    public key, appends a ``KEY_ROTATED`` event to ``ledger`` signed as the
    OLD device (it's appended before the key files are replaced), then
    atomically replaces the on-disk key files with the new key. Returns the
    new :class:`DeviceKey`.

    ``ledger`` must belong to the same device being rotated (its
    ``device_id`` should match the old key's, since the event needs to be
    appended under that identity before it changes).
    """
    if reason not in _VALID_REASONS:
        raise ValueError(f"reason must be one of {sorted(_VALID_REASONS)}, got {reason!r}")

    key_dir = Path(key_dir)
    old_key = load_or_create_device_key(key_dir)

    new_private = Ed25519PrivateKey.generate()
    new_key = DeviceKey(new_private)

    old_device_id = old_key.device_id
    new_device_id = new_key.device_id
    new_public_key = new_key.public_key_b64

    signed_fields = {
        "old_device_id": old_device_id,
        "new_device_id": new_device_id,
        "new_public_key": new_public_key,
    }
    cross_signature = old_key.sign(canonicalize(signed_fields))

    payload = {
        **signed_fields,
        "cross_signature": cross_signature,
        "reason": reason,
        "effective_epoch": now_ts_device(),
    }
    payload_hash = sha256_hex(canonicalize(payload))

    event = AgentEvent(
        schema_version="1",
        event_id=new_event_id(),
        device_id=old_device_id,
        session_id=ROTATION_SESSION_ID,
        seq=0,  # stamped by Ledger.append
        kind=EventKind.KEY_ROTATED.value,
        ts_device=now_ts_device(),
        ts_monotonic_ns=now_monotonic_ns(),
        tool_use_id=None,
        tool_name=None,
        payload=payload,
        payload_hash=payload_hash,
        model=None,
        provider="recorder",
    )
    entry = ledger.append(event)
    if entry is None:
        raise RuntimeError(
            "failed to append KEY_ROTATED event to the ledger — refusing to "
            "rotate the on-disk key without a recorded, cross-signed handoff"
        )

    # New key material must be the only thing left on disk afterwards — the
    # old raw private key bytes are never written out again, and
    # atomic_write_bytes handles the swap atomically (keys.py convention).
    _write_new_key(key_dir, new_private, new_key)

    return new_key


def _write_new_key(key_dir: Path, new_private: Ed25519PrivateKey, new_key: DeviceKey) -> None:
    from .keys import PRIVATE_KEY_FILENAME, PUBLIC_KEY_FILENAME, atomic_write_bytes

    key_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        key_dir / PRIVATE_KEY_FILENAME,
        new_private.private_bytes_raw(),
        mode=0o600,
        prefix=".devkey-",
    )
    atomic_write_bytes(
        key_dir / PUBLIC_KEY_FILENAME,
        (new_key.public_key_b64 + "\n").encode("ascii"),
        mode=0o644,
        prefix=".devpub-",
    )


def rotate_cli(argv: list[str] | None = None) -> int:
    """CLI entry point: ``byoai-recorder-rotate-key --key-dir <dir> [--reason <r>]
    --ledger <path>``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="byoai-recorder-rotate-key",
        description="Rotate this device's agent recorder key, cross-signed for chain continuity.",
    )
    parser.add_argument(
        "--key-dir",
        required=True,
        type=Path,
        help="Directory holding the device key files.",
    )
    parser.add_argument(
        "--ledger",
        required=True,
        type=Path,
        help="Path to this device's ledger SQLite file.",
    )
    parser.add_argument(
        "--reason",
        default="rotation",
        choices=sorted(_VALID_REASONS),
        help="Why the key is being rotated (default: rotation).",
    )
    args = parser.parse_args(argv)

    old_key = load_or_create_device_key(args.key_dir)
    ledger = Ledger(args.ledger, device_id=old_key.device_id)
    try:
        new_key = rotate_key(args.key_dir, ledger, reason=args.reason)
    finally:
        ledger.close()

    print(f"rotated device key: {old_key.device_id} -> {new_key.device_id}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(rotate_cli())

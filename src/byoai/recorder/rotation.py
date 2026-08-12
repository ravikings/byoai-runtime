"""Device key rotation and revocation for the agent recorder (spec §8.4).

Rotation replaces a device's Ed25519 keypair while preserving verifiable
continuity of the ledger's hash chain across the key boundary: the OLD key
cross-signs the NEW public key, the new key files are written to disk, and
only then is that cross-signature sealed into the ledger as a
``KEY_ROTATED`` event — the last thing the retiring identity signs. The key
files land before the event so a crash between them never strands a
ledger-committed device_id whose private key was never durably written.

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
from .schema import (
    EVENT_SCHEMA_VERSION,
    AgentEvent,
    EventKind,
    new_event_id,
    new_span_id,
    new_trace_id,
    now_monotonic_ns,
    now_ts_device,
)

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
    public key, atomically writes the new key files to disk, then appends a
    ``KEY_ROTATED`` event to ``ledger`` signed as the OLD device. The key
    files are written *before* the event is appended (not after) so a crash
    in between never loses the only copy of the new private key: if the
    event append fails or the process dies right after it, the new key
    material is still safely on disk and the ledger just has one un-recorded
    rotation to reconcile, rather than a KEY_ROTATED event whose
    new_device_id can never be produced again because its private key never
    made it to disk.

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
        schema_version=EVENT_SCHEMA_VERSION,
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
        # Device-level event, not tied to any agent run.
        trace_id=new_trace_id(),
        span_id=new_span_id(),
        parent_span_id=None,
        continues_from=None,
    )
    # Written before the event is appended: the new private key only ever
    # exists in memory until this call, so if the process dies before this
    # completes, no ledger record commits to a device_id whose key was lost.
    _write_new_key(key_dir, new_private, new_key)

    entry = ledger.append(event)
    if entry is None:
        raise RuntimeError(
            "failed to append KEY_ROTATED event to the ledger — the new key "
            "is already on disk, but the cross-signed handoff was not "
            "recorded; retry the append or investigate the ledger before "
            "using the new key"
        )

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

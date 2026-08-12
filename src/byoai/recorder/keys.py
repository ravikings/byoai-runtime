"""Device identity for the agent recorder.

One Ed25519 keypair per device. The private key is generated locally, written
once with mode 0600, and never leaves the machine — there is deliberately no
export path for it on :class:`DeviceKey`. The public half and a derived,
restart-stable ``device_id`` are the only things callers can read.

See spec section 8 (identity and enrollment).
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "DeviceKey",
    "InsecureKeyPermissions",
    "PENDING_ROTATION_DIRNAME",
    "PRIVATE_KEY_FILENAME",
    "PUBLIC_KEY_FILENAME",
    "SIG_PREFIX",
    "atomic_write_bytes",
    "derive_device_id",
    "load_or_create_device_key",
]
# _finish_pending_rotation is intentionally not exported (leading underscore,
# not in __all__): it is an internal helper shared between this module and
# rotation.py via direct import, not part of the public keys.py contract.

PRIVATE_KEY_FILENAME = "device_ed25519.key"
PUBLIC_KEY_FILENAME = "device_ed25519.pub"
SIG_PREFIX = "ed25519:"
# Shared with rotation.py: where a rotated-in key is staged before being
# promoted to the live filenames above.
PENDING_ROTATION_DIRNAME = ".pending-rotation"
# Written into PENDING_ROTATION_DIRNAME only *after* the KEY_ROTATED ledger
# event is durably appended, and only then. Its presence is what tells
# reconciliation "this staged key is confirmed, finish promoting it" as
# opposed to "this staged key is here because staging ran but the ledger
# append hasn't happened yet (or failed)" — those two cases must be handled
# differently: a merely-staged, unconfirmed key must be left alone (the old
# live key stays authoritative, exactly like today), while a confirmed one
# must always end up live, even across a crash. Content is the old device_id
# so reconciliation doesn't need to re-derive it from the (possibly already
# partially replaced) live key files.
PROMOTION_CONFIRMED_MARKER = ".promotion-confirmed"

_PRIVATE_KEY_MODE = 0o600
_DIR_MODE = 0o700
_RAW_PRIVATE_KEY_LEN = 32
# base32 of a sha256 digest is 56 chars; 26 keeps the id short while leaving
# ~130 bits of the digest, which is far past any collision concern.
_DEVICE_ID_LEN = 26


class InsecureKeyPermissions(PermissionError):
    """Raised when the private key file is readable by anyone but its owner."""


def derive_device_id(public_key_b64: str) -> str:
    """Derive the stable device id from the public key.

    Deterministic: the same public key always yields the same id, across
    processes and restarts. Uses the hash of the raw public key rather than the
    key bytes themselves so the id does not carry key material verbatim.
    """
    raw = base64.b64decode(public_key_b64, validate=True)
    digest = hashlib.sha256(raw).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
    return "dev_" + b32[:_DEVICE_ID_LEN]


class DeviceKey:
    """An Ed25519 device identity.

    The private key stays inside this object. There is no accessor, property or
    serialization hook that returns private key bytes — signing is the only
    operation it exposes.
    """

    __slots__ = ("_private_key", "_public_key_b64", "_device_id")

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        raw_public = private_key.public_key().public_bytes_raw()
        self._public_key_b64 = base64.b64encode(raw_public).decode("ascii")
        self._device_id = derive_device_id(self._public_key_b64)

    # -- identity ---------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def public_key_b64(self) -> str:
        return self._public_key_b64

    # -- signing ----------------------------------------------------------

    def sign(self, data: bytes) -> str:
        """Sign ``data``; returns ``"ed25519:<base64>"``."""
        sig = self._private_key.sign(data)
        return SIG_PREFIX + base64.b64encode(sig).decode("ascii")

    @staticmethod
    def verify(public_key_b64: str, data: bytes, sig: str) -> bool:
        """Verify a signature produced by :meth:`sign`. Never raises."""
        if not isinstance(sig, str) or not sig.startswith(SIG_PREFIX):
            return False
        try:
            raw_sig = base64.b64decode(sig[len(SIG_PREFIX) :], validate=True)
            raw_pub = base64.b64decode(public_key_b64, validate=True)
            public_key = Ed25519PublicKey.from_public_bytes(raw_pub)
        except (ValueError, TypeError):
            return False
        try:
            public_key.verify(raw_sig, data)
        except (InvalidSignature, ValueError, TypeError):
            return False
        return True

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"DeviceKey(device_id={self._device_id!r})"


def _mark_promotion_confirmed(directory: Path, *, old_device_id: str) -> None:
    """Durably record that the staged key in ``PENDING_ROTATION_DIRNAME`` is
    confirmed (its KEY_ROTATED event is already appended to the ledger) and
    must be promoted — finishing the job on a later call if this one is
    interrupted. Must only be called after the ledger append succeeds."""
    pending_dir = directory / PENDING_ROTATION_DIRNAME
    atomic_write_bytes(
        pending_dir / PROMOTION_CONFIRMED_MARKER,
        old_device_id.encode("ascii"),
        mode=0o600,
        prefix=".devpromo-",
    )


def _finish_pending_rotation(directory: Path, *, old_device_id: str | None = None) -> bool:
    """Finish promoting a staged rotation key into the live filenames, if one
    is staged *and confirmed* (see ``PROMOTION_CONFIRMED_MARKER``). Returns
    ``True`` if a promotion happened (or was already complete except for
    cleanup), ``False`` if there was nothing to do.

    Safe to call unconditionally and repeatedly (idempotent). Deliberately
    does **not** promote a merely-staged key that was never confirmed (e.g.
    staging succeeded but the ledger append then failed) — only the marker's
    presence, which is written after the ledger append succeeds, makes a
    staged key eligible for promotion. Without that gate, this function
    would wrongly "finish" a rotation that was never actually committed.

    Sequencing is chosen so the only step that changes what a concurrent or
    subsequent ``load_or_create_device_key`` call sees on disk is the final,
    single, atomic ``os.replace`` of the staged private key into the live
    path — there is never a window where the live private key file is
    simply absent:

    1. Copy (not move) the current live key bytes to the archive filenames,
       if a live key exists and hasn't already been archived. A copy is not
       destructive, so this step doesn't need to be atomic with anything
       else — worst case on a crash here, the old key is simply archived
       twice or not yet archived, and it is still live and loadable either
       way.
    2. Atomically replace the live *public* key with the staged one. Not
       security-critical (the public key is re-derivable from the private
       key and is a convenience file only), so its ordering relative to
       step 3 doesn't matter for correctness.
    3. Atomically replace the live *private* key with the staged one. This
       is the step that flips device identity, and ``os.replace`` is a
       single filesystem syscall, so there is no intermediate state where
       the live private key file is missing.
    4. Best-effort cleanup of the marker and the now-empty pending directory.
    """
    pending_dir = directory / PENDING_ROTATION_DIRNAME
    marker = pending_dir / PROMOTION_CONFIRMED_MARKER
    if not marker.exists():
        return False

    pending_private = pending_dir / PRIVATE_KEY_FILENAME
    pending_public = pending_dir / PUBLIC_KEY_FILENAME
    live_private = directory / PRIVATE_KEY_FILENAME
    live_public = directory / PUBLIC_KEY_FILENAME

    if old_device_id is None:
        try:
            old_device_id = marker.read_text().strip() or None
        except OSError:
            old_device_id = None

    # pending_private may already be gone if a previous call got as far as
    # promoting but crashed before cleanup; in that case there's nothing left
    # to promote, just cleanup below.
    if pending_private.exists():
        if live_private.exists():
            if old_device_id is None:
                try:
                    old_device_id = _load(live_private).device_id
                except (InsecureKeyPermissions, ValueError):
                    old_device_id = "unknown"
            archive_private = directory / f".rotated-{old_device_id}.{PRIVATE_KEY_FILENAME}"
            archive_public = directory / f".rotated-{old_device_id}.{PUBLIC_KEY_FILENAME}"
            if archive_private.exists():
                # old_device_id could not be resolved (e.g. "unknown", or a
                # genuine repeat id) and something is already archived under
                # that name — never skip the backup silently, disambiguate
                # instead so the previously-live key is never lost.
                suffix = 2
                while archive_private.exists():
                    archive_private = (
                        directory / f".rotated-{old_device_id}-{suffix}.{PRIVATE_KEY_FILENAME}"
                    )
                    archive_public = (
                        directory / f".rotated-{old_device_id}-{suffix}.{PUBLIC_KEY_FILENAME}"
                    )
                    suffix += 1
            shutil.copy2(live_private, archive_private)
            if live_public.exists():
                shutil.copy2(live_public, archive_public)

        if pending_public.exists():
            os.replace(pending_public, live_public)
        os.replace(pending_private, live_private)

    try:
        marker.unlink()
    except OSError:
        pass
    try:
        pending_dir.rmdir()
    except OSError:  # pragma: no cover - leftover files, harmless
        pass
    return True


def load_or_create_device_key(dir: Path | str) -> DeviceKey:  # noqa: A002 - contract name
    """Load the device key from ``dir``, creating one on first run.

    The private key file is created with mode 0600. On POSIX, loading refuses
    (raises :class:`InsecureKeyPermissions`) if the file is group- or
    world-accessible — a key anyone can read is not a device identity.

    Before doing either of those, this reconciles any interrupted key
    rotation: if a staged key is sitting in ``PENDING_ROTATION_DIRNAME``
    (left behind by a crash during :func:`rotation._promote_staged_key`,
    which by the time that staged key exists has *already* durably
    committed a ``KEY_ROTATED`` event to the ledger), promotion is finished
    here before anything else happens. This is the guarantee that closes the
    original bug: once a rotation's ``KEY_ROTATED`` event is committed,
    there is no code path — no matter where a crash lands — that falls
    through to generating a brand-new random keypair instead of using the
    already cross-signed staged key. Without this, a crash between
    promoting the private and public key files could leave no live private
    key file at all, and the old (pre-reconciliation) code below would
    silently generate a fresh, never-cross-signed identity, orphaning both
    the archived old key and the staged new key.
    """
    directory = Path(dir)
    key_path = directory / PRIVATE_KEY_FILENAME
    _finish_pending_rotation(directory)
    if key_path.exists():
        return _load(key_path)

    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            directory.chmod(_DIR_MODE)
        except OSError:  # pragma: no cover - unusual filesystems
            pass

    private_key = Ed25519PrivateKey.generate()
    _write_private_key(key_path, private_key)

    device_key = DeviceKey(private_key)
    # Convenience only: the verifier can be pointed at this instead of being
    # handed the key out of band. Losing it costs nothing. Written the same
    # atomic way as the private key (and as rotation.py's own key writes) so
    # a crash mid-write can't leave a truncated public key file behind.
    atomic_write_bytes(
        directory / PUBLIC_KEY_FILENAME,
        (device_key.public_key_b64 + "\n").encode("ascii"),
        mode=0o644,
        prefix=".devpub-",
    )
    return device_key


def atomic_write_bytes(path: Path, data: bytes, *, mode: int, prefix: str) -> None:
    """Write ``data`` to ``path`` atomically (tmp file + rename) at file
    permission ``mode``, so a crash or a full disk mid-write can never leave
    a truncated or wide-open file behind."""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=prefix)
    tmp_path = Path(tmp_name)
    try:
        try:
            os.chmod(fd if os.name == "posix" else tmp_name, mode)
        except BaseException:
            # fdopen() below would normally take ownership of fd and close
            # it; since we haven't reached it yet, close it ourselves.
            os.close(fd)
            raise
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        path.chmod(mode)


def _write_private_key(key_path: Path, private_key: Ed25519PrivateKey) -> None:
    """Write the raw private key atomically, never wider than 0600."""
    atomic_write_bytes(
        key_path, private_key.private_bytes_raw(), mode=_PRIVATE_KEY_MODE, prefix=".devkey-"
    )


def _load(key_path: Path) -> DeviceKey:
    _check_permissions(key_path)
    raw = key_path.read_bytes()
    if len(raw) != _RAW_PRIVATE_KEY_LEN:
        raise ValueError(
            f"device key at {key_path} is {len(raw)} bytes, expected "
            f"{_RAW_PRIVATE_KEY_LEN} — file is corrupt or not a device key"
        )
    return DeviceKey(Ed25519PrivateKey.from_private_bytes(raw))


def _check_permissions(key_path: Path) -> None:
    if os.name != "posix":
        return
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode & ~_PRIVATE_KEY_MODE:
        raise InsecureKeyPermissions(
            f"device key {key_path} has mode {mode:04o}; refusing to load a key "
            f"with permissions wider than {_PRIVATE_KEY_MODE:04o}. "
            f"Fix with: chmod 600 {key_path}"
        )

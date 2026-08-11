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
    "PRIVATE_KEY_FILENAME",
    "PUBLIC_KEY_FILENAME",
    "SIG_PREFIX",
    "atomic_write_bytes",
    "derive_device_id",
    "load_or_create_device_key",
]

PRIVATE_KEY_FILENAME = "device_ed25519.key"
PUBLIC_KEY_FILENAME = "device_ed25519.pub"
SIG_PREFIX = "ed25519:"

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


def load_or_create_device_key(dir: Path | str) -> DeviceKey:  # noqa: A002 - contract name
    """Load the device key from ``dir``, creating one on first run.

    The private key file is created with mode 0600. On POSIX, loading refuses
    (raises :class:`InsecureKeyPermissions`) if the file is group- or
    world-accessible — a key anyone can read is not a device identity.
    """
    directory = Path(dir)
    key_path = directory / PRIVATE_KEY_FILENAME
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

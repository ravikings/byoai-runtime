"""One resolver for "who is this host, to Coriqo?".

Two credentials can reach Coriqo from an agent host, and they are not
interchangeable:

* the **device key** — an Ed25519 keypair generated on this machine by
  :mod:`byoai.recorder.keys`, bound to a ``device_id`` by a one-time
  enrollment token (:mod:`byoai.recorder.enroll`). The private half never
  leaves the process, and possession of it is what a signature proves.
* a **static API key** — :class:`~byoai.recorder.coriqo_agents.CoriqoCredentials`,
  read from ``BYOAI_CORIQO_API_KEY`` and sent as a header. A long-lived bearer
  secret sitting in the agent's own environment, and one that needs
  ``governance:approve`` to register agents.

That difference stops being academic once the runtime enforces mandates. The
credential that fetches an agent's permitted-tool scope is the credential that
decides what the agent may do, so an agent holding a static key that can
approve governance changes holds the key to its own cage. Enforcement
therefore authenticates with the device key only; the static key stays
supported for publishing, which is what it has always been used for.

:func:`resolve_identity` picks between them, preferring the device key, and
returns ``None`` when neither is configured — a normal state, not an error.
Key material stays behind :class:`Signer`: this module holds a signer object,
never raw bytes, so on-disk key handling lives in exactly one place
(``keys.py``) and tests can inject a fake signer without touching a filesystem.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from byoai.errors import CoriqoIdentityError, EnforcementIdentityUnavailableError

from .coriqo_agents import CoriqoCredentials
from .enroll import ENROLLMENT_FILENAME, EnrollmentError, load_enrollment_state

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .keys import DeviceKey

__all__ = [
    "CoriqoIdentity",
    "DeviceKeySigner",
    "IdentitySource",
    "Signer",
    "default_key_dir",
    "reset_identity_warning_for_tests",
    "resolve_identity",
]

log = logging.getLogger(__name__)

#: Named in the legacy-credentials warning and in the error raised when
#: enforcement has no device identity to work with, so the message tells the
#: operator what to actually run.
ENROLL_COMMAND = "byoai-recorder-enroll --coriqo-url <url> --token <token> --key-dir <dir>"

_warned_lock = threading.Lock()
_warned_legacy = False
_warned_tenantless = False


@runtime_checkable
class Signer(Protocol):
    """Produces an Ed25519 signature over request material.

    Deliberately narrow: a signer exposes its public half and can sign. There
    is no accessor for private key bytes anywhere in this contract, matching
    :class:`~byoai.recorder.keys.DeviceKey` itself.
    """

    @property
    def device_id(self) -> str: ...

    @property
    def public_key_b64(self) -> str: ...

    def sign(self, data: bytes) -> str:
        """Return ``"ed25519:<base64>"`` over ``data``."""
        ...


class IdentitySource:
    """Where a resolved identity came from."""

    DEVICE = "device"
    API_KEY = "api_key"


class DeviceKeySigner:
    """A :class:`Signer` backed by the on-disk device key.

    The key is loaded on first use rather than at resolution time, so building
    an identity never trips the permission check for a caller that only wanted
    to know whether a device identity exists.
    """

    __slots__ = ("_key_dir", "_key", "_lock")

    def __init__(self, key_dir: Path | str) -> None:
        self._key_dir = Path(key_dir)
        self._key: DeviceKey | None = None
        # First use can be concurrent (one signer, several request threads).
        # Single-flight the load so the permission check and the rotation
        # reconciliation inside it run once, not once per racing thread.
        self._lock = threading.Lock()

    @property
    def key_dir(self) -> Path:
        return self._key_dir

    def _load(self) -> DeviceKey:
        key = self._key
        if key is not None:
            return key
        with self._lock:
            if self._key is not None:
                return self._key
            # Imported here, not at module import: keys.py needs
            # `cryptography`, which lives behind the `byoai-runtime[recorder]`
            # extra, and a base install must still be able to import this
            # module.
            from .keys import InsecureKeyPermissions, load_device_key

            try:
                loaded = load_device_key(self._key_dir)
            except (InsecureKeyPermissions, ValueError) as exc:
                raise CoriqoIdentityError(
                    f"device key at {self._key_dir} is unusable: {exc}"
                ) from exc
            if loaded is None:
                # Deliberately the load-only entry point:
                # load_or_create_device_key() would mint a fresh keypair here,
                # which for an enrolled device is wrong twice over — the new
                # key is bound to no device_id, and the silent replacement
                # hides that the enrolled identity is gone. Checking the file
                # first and then calling the creating loader would leave that
                # guarantee resting on a TOCTOU window; there is no create
                # path to race here.
                raise CoriqoIdentityError(
                    f"{self._key_dir} has enrollment state but no device key. "
                    f"The enrolled identity cannot sign. Re-enroll with: {ENROLL_COMMAND}"
                )
            self._key = loaded
            return loaded

    @property
    def device_id(self) -> str:
        return self._load().device_id

    @property
    def public_key_b64(self) -> str:
        return self._load().public_key_b64

    def sign(self, data: bytes) -> str:
        return self._load().sign(data)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"DeviceKeySigner(key_dir={str(self._key_dir)!r})"


@dataclass(frozen=True, slots=True)
class CoriqoIdentity:
    """How this host authenticates to Coriqo, and what that identity may do.

    ``enforcement_capable`` is the whole point of the type: it is ``True`` only
    for a device-backed identity, and enforcement callers should reach for
    :meth:`require_enforcement` rather than re-checking the flag themselves.
    """

    base_url: str
    source: str
    signer: Signer | None = None
    enrolled_device_id: str | None = None
    credentials: CoriqoCredentials | None = None
    #: Tenant this identity acts in, for Coriqo's ``X-Tenant-Slug`` header.
    #: Taken from the static credentials, or from ``enrollment.json`` for a
    #: device. ``None`` on a device enrolled before the tenant was persisted;
    #: callers fall back to ``BYOAI_CORIQO_TENANT_SLUG``.
    tenant_slug: str | None = None

    @property
    def enforcement_capable(self) -> bool:
        return self.source == IdentitySource.DEVICE and self.signer is not None

    @property
    def device_id(self) -> str | None:
        """The id of the key that actually signs, not the id on disk.

        Those two can disagree: ``byoai-recorder-rotate-key`` replaces the live
        key — and so the derived device id — without rewriting
        ``enrollment.json``. Reporting the enrolled id while signing with the
        rotated key would send Coriqo a claim and a proof about two different
        keys, so the signer is authoritative here and
        :attr:`enrolled_device_id` keeps the original for anyone who needs to
        follow the rotation back through the ledger's ``KEY_ROTATED`` event.
        """
        if self.signer is not None:
            return self.signer.device_id
        return self.enrolled_device_id

    @classmethod
    def from_device(
        cls,
        *,
        base_url: str,
        device_id: str,
        signer: Signer,
        tenant_slug: str | None = None,
    ) -> CoriqoIdentity:
        return cls(
            base_url=base_url.rstrip("/"),
            source=IdentitySource.DEVICE,
            signer=signer,
            enrolled_device_id=device_id,
            tenant_slug=tenant_slug,
        )

    @classmethod
    def from_credentials(cls, credentials: CoriqoCredentials) -> CoriqoIdentity:
        return cls(
            base_url=credentials.base_url.rstrip("/"),
            source=IdentitySource.API_KEY,
            credentials=credentials,
            tenant_slug=credentials.tenant_slug,
        )

    def sign(self, data: bytes) -> str:
        """Sign ``data`` with the device key.

        Raises :class:`EnforcementIdentityUnavailableError` on a legacy
        API-key identity, which has nothing to sign with.
        """
        signer = self.require_enforcement()
        return signer.sign(data)

    def require_enforcement(self) -> Signer:
        """Return the signer, or explain why enforcement can't proceed.

        Enforcement callers use this instead of branching on
        :attr:`enforcement_capable`, so the "you have a publish-only identity"
        message is written once and says what to run.
        """
        if self.signer is None or self.source != IdentitySource.DEVICE:
            raise EnforcementIdentityUnavailableError(
                "mandate enforcement requires a device-enrolled Coriqo identity, but "
                f"this host has only a static API key ({self.base_url}). A static key "
                "cannot sign enforcement requests, and one carrying governance:approve "
                "would let the agent edit its own mandate. Enroll this device with: "
                f"{ENROLL_COMMAND}"
            )
        return self.signer


def default_key_dir() -> Path:
    """The recorder's key/enrollment directory: ``BYOAI_RECORDER_DIR``,
    defaulting to ``~/.byoai/recorder`` — the same directory
    :class:`~byoai.recorder.integration.Recorder` uses, so enrolling a device
    and recording with it never disagree about where the key lives."""
    return Path(os.getenv("BYOAI_RECORDER_DIR", str(Path.home() / ".byoai" / "recorder")))


def resolve_identity(*, key_dir: Path | str | None = None) -> CoriqoIdentity | None:
    """Resolve this host's Coriqo identity, preferring the device key.

    Order:

    1. device enrollment state under ``key_dir`` — enforcement-capable;
    2. :meth:`CoriqoCredentials.from_env` — publish-only, warned about once
       per process rather than once per call, so a per-request caller doesn't
       turn a configuration note into a log flood;
    3. ``None`` — nothing configured, which is supported: callers no-op.

    Raises :class:`CoriqoIdentityError` if enrollment state exists but is
    unreadable. That is a broken install, not an absent one, and silently
    downgrading it to the static key would be the exact substitution this
    resolver exists to prevent.
    """
    directory = Path(key_dir) if key_dir is not None else default_key_dir()

    try:
        state = load_enrollment_state(directory)
    except EnrollmentError as exc:
        raise CoriqoIdentityError(
            f"{directory / ENROLLMENT_FILENAME} exists but cannot be read: {exc}"
        ) from exc

    if state is not None:
        if state.tenant_slug is None:
            _warn_tenantless_enrollment_once(directory)
        return CoriqoIdentity.from_device(
            base_url=state.coriqo_base_url,
            device_id=state.device_id,
            signer=DeviceKeySigner(directory),
            tenant_slug=state.tenant_slug,
        )

    credentials = CoriqoCredentials.from_env()
    if credentials is not None:
        _warn_legacy_once()
        return CoriqoIdentity.from_credentials(credentials)

    return None


def _warn_legacy_once() -> None:
    global _warned_legacy
    with _warned_lock:
        if _warned_legacy:
            return
        _warned_legacy = True
    log.warning(
        "Using the static BYOAI_CORIQO_API_KEY to reach Coriqo. This identity can "
        "publish runs but cannot be used for mandate enforcement — enforcement has "
        "to be signed by a device key so the agent host can't authorize its own "
        "scope. Enroll this device with: %s",
        ENROLL_COMMAND,
    )


def _warn_tenantless_enrollment_once(directory: Path) -> None:
    """Say once that this device predates the persisted tenant.

    Not an error: the device identity is intact and signs fine, it just can't
    name its own tenant, so an enforcement caller still needs
    ``BYOAI_CORIQO_TENANT_SLUG``. Once per process, like the legacy-credentials
    warning — this is read on a refresh interval, and one note per refresh
    would be a log flood.
    """
    global _warned_tenantless
    with _warned_lock:
        if _warned_tenantless:
            return
        _warned_tenantless = True
    log.warning(
        "%s was written before the tenant was recorded, so enforcement requests "
        "still need BYOAI_CORIQO_TENANT_SLUG (or an explicit tenant_slug=). "
        "Re-enroll to persist it: %s --tenant-slug <slug> --force",
        directory / ENROLLMENT_FILENAME,
        ENROLL_COMMAND,
    )


def reset_identity_warning_for_tests() -> None:
    """Re-arm the once-per-process legacy and tenantless warnings. Tests only."""
    global _warned_legacy, _warned_tenantless
    with _warned_lock:
        _warned_legacy = False
        _warned_tenantless = False

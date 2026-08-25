"""Device enrollment for the agent recorder — workstream F.

One-shot, CLI-driven flow (spec section 8.2): a device generates (or reuses)
its Ed25519 keypair locally via :func:`load_or_create_device_key`, then POSTs
only the public key and a single-use enrollment token to Coriqo. Coriqo
replies with a ``device_id`` that this device is now bound to. The private
key never leaves :class:`~byoai.recorder.keys.DeviceKey` — it is never read
off disk here, never placed in a request body, never logged.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from byoai.recorder.keys import (
    InsecureKeyPermissions,
    atomic_write_bytes,
    load_or_create_device_key,
)
from byoai.recorder.schema import now_ts_device

__all__ = [
    "ENROLLMENT_FILENAME",
    "EnrollmentError",
    "EnrollmentState",
    "enroll",
    "enroll_cli",
    "load_enrollment_state",
]

ENROLLMENT_FILENAME = "enrollment.json"
_ENROLLMENT_MODE = 0o600


@dataclass(frozen=True, slots=True)
class EnrollmentState:
    device_id: str
    coriqo_base_url: str
    enrolled_at: str  # RFC3339 UTC
    #: Tenant this device belongs to, sent as ``X-Tenant-Slug`` on enforcement
    #: requests. ``None`` for state written before the field existed — see
    #: :func:`load_enrollment_state`.
    tenant_slug: str | None = None


class EnrollmentError(RuntimeError):
    """Enrollment token rejected, network failure, or already-enrolled conflict."""


def enroll(
    *,
    coriqo_base_url: str,
    token: str,
    key_dir: Path,
    tenant_slug: str | None = None,
    http_client: httpx.Client | None = None,
    force: bool = False,
) -> EnrollmentState:
    """Enroll this device with Coriqo and persist the resulting state.

    Idempotent: if ``enrollment.json`` already exists under ``key_dir`` and
    ``force`` is not set, the existing state is returned without a network
    call. The device's Ed25519 keypair is created on first use via
    :func:`load_or_create_device_key`; only its public half
    (``key.public_key_b64``) is ever placed in the request body.

    ``tenant_slug`` records which Coriqo tenant this device belongs to, so
    enforcement requests can set ``X-Tenant-Slug`` without a separate env var.
    A ``tenant_slug`` in the enrollment response wins over the argument: the
    server issued the token and knows which tenant it was issued for.
    """
    key_dir = Path(key_dir)

    if not force:
        existing = load_enrollment_state(key_dir)
        if existing is not None:
            return existing

    key = load_or_create_device_key(key_dir)

    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.Client()
    try:
        try:
            response = client.post(
                f"{coriqo_base_url.rstrip('/')}/v1/enroll",
                json={"public_key": key.public_key_b64, "token": token},
            )
        except httpx.HTTPError as exc:
            raise EnrollmentError(f"enrollment request failed: {exc}") from exc

        if response.status_code not in (200, 201):
            raise EnrollmentError(
                f"enrollment rejected: HTTP {response.status_code} {response.text}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise EnrollmentError(
                f"enrollment response was not valid JSON: {response.text}"
            ) from exc

        device_id = body.get("device_id")
        if not device_id:
            raise EnrollmentError(
                f"enrollment response missing device_id: {response.text}"
            )
    finally:
        if owns_client:
            client.close()

    state = EnrollmentState(
        device_id=device_id,
        coriqo_base_url=body.get("coriqo_base_url") or coriqo_base_url,
        enrolled_at=now_ts_device(),
        tenant_slug=body.get("tenant_slug") or tenant_slug or None,
    )
    _write_enrollment_state(key_dir, state)
    return state


def load_enrollment_state(key_dir: Path) -> EnrollmentState | None:
    """Load previously persisted enrollment state, if any.

    Raises ``EnrollmentError`` (rather than a raw ``json.JSONDecodeError``
    or ``KeyError``) if ``enrollment.json`` exists but is truncated,
    corrupt, or missing an expected field, so callers can distinguish
    "not enrolled yet" (``None``) from "enrolled state is unreadable."
    """
    state_path = Path(key_dir) / ENROLLMENT_FILENAME
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text())
        return EnrollmentState(
            device_id=data["device_id"],
            coriqo_base_url=data["coriqo_base_url"],
            enrolled_at=data["enrolled_at"],
            # .get, not []: enrollment.json written before this field existed
            # is still a valid enrolled device. It loads with tenant_slug None
            # and falls back to BYOAI_CORIQO_TENANT_SLUG, rather than crashing
            # or — worse — looking unenrolled and minting a second identity.
            tenant_slug=data.get("tenant_slug"),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise EnrollmentError(f"enrollment state at {state_path} is corrupt: {exc}") from exc


def _write_enrollment_state(key_dir: Path, state: EnrollmentState) -> None:
    """Write ``enrollment.json`` atomically (tmp file + rename) so a crash or
    a full disk mid-write can never leave a truncated, unparseable file
    behind."""
    key_dir = Path(key_dir)
    key_dir.mkdir(parents=True, exist_ok=True)
    state_path = key_dir / ENROLLMENT_FILENAME
    atomic_write_bytes(
        state_path,
        json.dumps(asdict(state)).encode("utf-8"),
        mode=_ENROLLMENT_MODE,
        prefix=".enrollment-",
    )


def enroll_cli(argv: list[str] | None = None) -> int:
    """CLI entry point: ``byoai-recorder-enroll --coriqo-url <url> --token <tok>
    --key-dir <dir>``."""
    parser = argparse.ArgumentParser(
        prog="byoai-recorder-enroll",
        description="Enroll this device with Coriqo for agent recorder ingestion.",
    )
    parser.add_argument(
        "--coriqo-url",
        required=True,
        help="Base URL of the Coriqo ingest service, e.g. https://coriqo.example.com",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="Single-use enrollment token issued by Coriqo (cik_live_...).",
    )
    parser.add_argument(
        "--key-dir",
        required=True,
        type=Path,
        help="Directory holding the device key and enrollment state.",
    )
    parser.add_argument(
        "--tenant-slug",
        default=None,
        help="Coriqo tenant this device belongs to, e.g. acme_bank. Recorded in "
             "enrollment.json and sent as X-Tenant-Slug on enforcement requests. "
             "A tenant returned by Coriqo takes precedence.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-enroll even if enrollment state already exists.",
    )
    args = parser.parse_args(argv)

    try:
        state = enroll(
            coriqo_base_url=args.coriqo_url,
            token=args.token,
            key_dir=args.key_dir,
            tenant_slug=args.tenant_slug,
            force=args.force,
        )
    except (EnrollmentError, InsecureKeyPermissions, ValueError) as exc:
        # ValueError covers a corrupt or wrong-length device key on disk, which
        # load_or_create_device_key raises from inside enroll(). A truncated
        # key file is exactly the case someone runs this command to recover
        # from, so it has to reach the same clean "enrollment failed" exit as
        # every other failure rather than a raw traceback.
        print(f"enrollment failed: {exc}", file=sys.stderr)
        return 1

    where = state.coriqo_base_url
    if state.tenant_slug:
        where = f"{where} (tenant {state.tenant_slug})"
    print(f"enrolled device {state.device_id} at {where}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(enroll_cli())

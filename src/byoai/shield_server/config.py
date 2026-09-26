"""Configuration for the self-hosted Shield server (Phase 1.5).

Every knob is an env var (``internal_doc/shield_msp_plan.md``, "Config"
table), so ``byoai-shield-server serve`` needs no flags for the common case.
The data directory holds everything this server owns: two small SQLite
databases, the Ed25519 signing key, and the admin token — nothing here ever
touches message text, which the server never receives in the first place.
"""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_ORG",
    "DEFAULT_PORT",
    "ShieldServerConfig",
    "admin_token_path",
    "load_config",
]

DEFAULT_HOST = "127.0.0.1"
#: Not 17831 (Shield's own local port) and not 17840-adjacent ports other
#: ByoAI tools use — a dedicated port for a dedicated process.
DEFAULT_PORT = 17840
DEFAULT_ORG = "default"

ADMIN_TOKEN_FILENAME = "admin_token"


def _default_data_dir() -> Path:
    return Path.home() / ".byoai" / "shield-server"


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw and raw.strip() else default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if 1 <= value <= 65535 else default


@dataclass(frozen=True, slots=True)
class ShieldServerConfig:
    data_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    public_url: str = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
    org: str = DEFAULT_ORG
    #: Plaintext admin bearer token. Held only in memory plus its own 0600
    #: file under ``data_dir`` — never in the enrolment or policy databases.
    admin_token: str = ""

    @property
    def ingest_db_path(self) -> Path:
        return self.data_dir / "ingest.db"

    @property
    def server_db_path(self) -> Path:
        return self.data_dir / "shield_server.db"

    @property
    def signing_key_path(self) -> Path:
        return self.data_dir / "signing_key.ed25519"


def admin_token_path(data_dir: Path) -> Path:
    return data_dir / ADMIN_TOKEN_FILENAME


def _generate_admin_token() -> str:
    # 32 random bytes, url-safe: pasteable into a header or a shell one-liner
    # with no quoting surprises.
    return secrets.token_urlsafe(32)


def _load_or_create_admin_token(data_dir: Path) -> tuple[str, bool]:
    """The admin token, creating and persisting one (0600) on first run.

    Returns ``(token, freshly_generated)`` so the CLI can print it exactly
    once, on the run that minted it, and never again — same posture as an
    enrolment token.
    """
    env_token = os.environ.get("BYOAI_SHIELD_SERVER_ADMIN_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip(), False
    path = admin_token_path(data_dir)
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token, False
    token = _generate_admin_token()
    data_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            data_dir.chmod(0o700)
        except OSError:  # pragma: no cover - unusual filesystems
            pass
    path.write_text(token + "\n")
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass
        else:
            # Belt-and-suspenders: confirm the mode actually stuck (some
            # filesystems / containers ignore chmod silently).
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & ~0o600:
                path.chmod(0o600)
    return token, True


def load_config(*, generate_admin_token: bool = True) -> tuple[ShieldServerConfig, bool]:
    """Build config from the environment, creating the data dir and (unless
    ``generate_admin_token`` is False) the admin token file on first run.

    Returns ``(config, admin_token_is_fresh)``.
    """
    data_dir = _env_path("BYOAI_SHIELD_SERVER_DATA", _default_data_dir())
    host = _env_str("BYOAI_SHIELD_SERVER_HOST", DEFAULT_HOST)
    port = _env_int("BYOAI_SHIELD_SERVER_PORT", DEFAULT_PORT)
    org = _env_str("BYOAI_SHIELD_SERVER_ORG", DEFAULT_ORG)
    public_url = _env_str("BYOAI_SHIELD_SERVER_PUBLIC_URL", f"http://{host}:{port}")

    admin_token = ""
    fresh = False
    if generate_admin_token:
        admin_token, fresh = _load_or_create_admin_token(data_dir)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)

    cfg = ShieldServerConfig(
        data_dir=data_dir, host=host, port=port, public_url=public_url,
        org=org, admin_token=admin_token,
    )
    return cfg, fresh

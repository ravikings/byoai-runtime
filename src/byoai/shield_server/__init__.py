"""Self-hosted Shield server — free, single-org control plane.

An admin with no Coriqo account runs one command, enrols their Macs, sees
the fleet and sets policy. Speaks the exact same wire protocol as Coriqo's
managed mode, so the released ``byoai.integrations.shield_publish.Publisher``
works against this server completely unchanged — only the address it enrols
against differs.

See :func:`byoai.shield_server.app.create_app` and the console script
``byoai-shield-server`` (``byoai.shield_server.cli:main``).
"""

from __future__ import annotations

from byoai.shield_server.app import create_app
from byoai.shield_server.config import ShieldServerConfig, load_config

__all__ = ["ShieldServerConfig", "create_app", "load_config"]

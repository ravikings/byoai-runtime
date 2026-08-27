"""Read-only HTTP surface over the ingest read model.

Mounted into the context-cache proxy app so one ``pip install`` and one port
serve both the API and the console that reads it.

Read-only by construction: this router never writes to the ledger or the
ingest store. Evidence arrives through the ingest path, which authenticates
devices and verifies signatures; nothing here can add to it, amend it, or
mark it verified.
"""

from .router import build_console_router

__all__ = ["build_console_router"]

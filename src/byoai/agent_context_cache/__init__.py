"""agent_context_cache — Anthropic-compatible proxy with context caching.

Sits in front of the Anthropic Messages API (or an OpenAI-compatible
backend, via `openai_compat`), deduplicating repeated context across a
session and persisting durable usage/benchmark records via `db`.

The FastAPI app is built lazily on first access so importing this module
doesn't require the `fastapi`/`redis` extras unless the app is actually
used.
"""

from typing import Any

__all__ = ["get_app"]


def get_app() -> Any:
    """Return the FastAPI app, built on first call.

    Requires the `fastapi` extra (`pip install byoai-runtime[fastapi]`)
    and a reachable Redis instance (see `BYOAI_REDIS_URL`).
    """
    from .main import app

    return app

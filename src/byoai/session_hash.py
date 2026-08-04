"""Session-scoped hash stores for content dedup.

The ``SessionDedup`` stage records SHA-256 hashes of large text blocks it has
already seen *within a single conversation* so a later turn that resends the
same file snapshot can be collapsed to a placeholder. That state is keyed by
``session_id`` and must never leak across sessions (see
``docs`` / the proxy's ``derive_session_id`` correctness argument).

Two implementations:

* :class:`InMemoryHashStore` — a bounded, TTL'd dict fallback that needs no
  external service. Suitable for single-process deployments and as the
  automatic fallback when Redis is unreachable.
* :class:`RedisHashStore` — backed by ``byoai:hashes:{session_id}`` SET keys
  with an idle TTL refreshed on every touch, so an active conversation keeps
  its dedup state while a quiet one is reaped. Degrades to an in-memory
  fallback on any Redis error rather than losing dedup entirely.

These port the proxy's ``is_duplicate_hash`` / ``add_hash`` /
``_local_get_session`` behavior verbatim so the ``SessionDedup`` stage is a
drop-in for the legacy inline logic.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

# Default idle TTL for a session's dedup set: 8h, matching the proxy's
# BYOAI_SESSION_TTL_SECONDS default. An active conversation refreshes this on
# every touch; a conversation that goes quiet is reaped automatically.
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60

# Cap on distinct sessions the in-memory store keeps; oldest-touched evicted.
DEFAULT_LOCAL_SESSION_MAX = 500


class SessionHashStore(ABC):
    """Records and queries content hashes scoped to a ``session_id``."""

    @abstractmethod
    async def is_duplicate(self, session_id: str, doc_hash: str) -> bool:
        """True if ``doc_hash`` was already recorded for ``session_id``."""

    @abstractmethod
    async def add(self, session_id: str, doc_hash: str) -> None:
        """Record ``doc_hash`` as seen for ``session_id``."""


class InMemoryHashStore(SessionHashStore):
    """Bounded, TTL'd in-process store.

    Ports the proxy's ``_local_get_session`` / ``_prune_local_sessions``:
    a session's set is lazily reset once it is older than the TTL, and the
    total number of tracked sessions is capped, evicting the oldest-touched
    first.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        max_sessions: int = DEFAULT_LOCAL_SESSION_MAX,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, dict[str, Any]] = {}

    def _prune(self) -> None:
        if len(self._sessions) <= self.max_sessions:
            return
        # Drop the oldest-touched sessions first.
        ordered = sorted(self._sessions.items(), key=lambda kv: kv[1]["touched"])
        for sid, _ in ordered[: len(ordered) - self.max_sessions]:
            self._sessions.pop(sid, None)

    def _get_session(self, session_id: str) -> set[str]:
        now = time.time()
        entry = self._sessions.get(session_id)
        if entry is None or (now - entry["touched"]) > self.ttl_seconds:
            entry = {"hashes": set(), "touched": now}
            self._sessions[session_id] = entry
            self._prune()
        entry["touched"] = now
        return entry["hashes"]

    async def is_duplicate(self, session_id: str, doc_hash: str) -> bool:
        return doc_hash in self._get_session(session_id)

    async def add(self, session_id: str, doc_hash: str) -> None:
        self._get_session(session_id).add(doc_hash)


class RedisHashStore(SessionHashStore):
    """Redis/Valkey-backed store with an in-memory fallback.

    ``client`` is an async redis-py client (e.g. from
    ``byoai.cache.redis.make_redis_client``). On any Redis error the store
    transparently falls back to an :class:`InMemoryHashStore`, so a Redis
    outage degrades dedup quality rather than breaking the request path —
    identical to the proxy's original behavior.
    """

    def __init__(
        self,
        client: Any,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        key_prefix: str = "byoai:hashes:",
        fallback: InMemoryHashStore | None = None,
    ) -> None:
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self.fallback = fallback or InMemoryHashStore(ttl_seconds=ttl_seconds)

    def _key(self, session_id: str) -> str:
        return f"{self.key_prefix}{session_id}"

    async def is_duplicate(self, session_id: str, doc_hash: str) -> bool:
        key = self._key(session_id)
        try:
            is_member = bool(await self.client.sismember(key, doc_hash))
            # Refresh idle TTL on every touch so an active conversation never
            # loses its dedup state mid-stream, while a conversation that goes
            # quiet gets reaped automatically.
            await self.client.expire(key, self.ttl_seconds)
            return is_member
        except Exception:
            return await self.fallback.is_duplicate(session_id, doc_hash)

    async def add(self, session_id: str, doc_hash: str) -> None:
        # Always mirror into the local fallback so a subsequent Redis outage
        # still sees what this process recorded.
        await self.fallback.add(session_id, doc_hash)
        key = self._key(session_id)
        try:
            await self.client.sadd(key, doc_hash)
            await self.client.expire(key, self.ttl_seconds)
        except Exception:
            pass

"""Session-scoped hash stores for content dedup.

The ``SessionDedup`` stage records SHA-256 hashes of large text blocks it has
already seen *within a single conversation* so a later turn that resends the
same file snapshot can be collapsed to a placeholder. That state is keyed by
``session_id`` and must never leak across sessions (see
``docs`` / the proxy's ``derive_session_id`` correctness argument).

Two implementations:

* :class:`InMemoryHashStore` — a bounded, TTL'd dict fallback that needs no
  external service. Suitable for single-process deployments and as the
  automatic fallback when Redis is unreachable. Bounded on both axes: the
  number of tracked sessions *and* the hashes kept per session, so a
  long-running proxy has a hard RAM ceiling instead of growing with uptime.
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
from collections import OrderedDict
from typing import Any

# Default idle TTL for a session's dedup set: 8h, matching the proxy's
# BYOAI_SESSION_TTL_SECONDS default. An active conversation refreshes this on
# every touch; a conversation that goes quiet is reaped automatically.
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60

# Cap on distinct sessions the in-memory store keeps; oldest-touched evicted.
DEFAULT_LOCAL_SESSION_MAX = 500

# Cap on hashes retained per session; oldest-added evicted first (FIFO).
#
# Without this the session cap above only bounds one axis: a single marathon
# conversation could accumulate hashes for its entire 8h TTL, so peak RAM grew
# with uptime rather than being a fixed ceiling. Evicting the oldest hash costs
# at most one re-send of a block that hasn't been seen in a long time — dedup
# quality degrades gracefully, which is the same trade this class already makes
# for the session cap and the TTL.
DEFAULT_MAX_HASHES_PER_SESSION = 5_000


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
    first. Additionally caps the hashes held per session (FIFO), so total
    memory is bounded by ``max_sessions * max_hashes_per_session`` regardless
    of how long the process runs or how long any one conversation lasts.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        max_sessions: int = DEFAULT_LOCAL_SESSION_MAX,
        max_hashes_per_session: int = DEFAULT_MAX_HASHES_PER_SESSION,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self.max_hashes_per_session = max_hashes_per_session
        self._sessions: dict[str, dict[str, Any]] = {}

    def _prune(self) -> None:
        if len(self._sessions) <= self.max_sessions:
            return
        # Drop the oldest-touched sessions first.
        ordered = sorted(self._sessions.items(), key=lambda kv: kv[1]["touched"])
        for sid, _ in ordered[: len(ordered) - self.max_sessions]:
            self._sessions.pop(sid, None)

    def _get_session(self, session_id: str) -> OrderedDict[str, None]:
        # An OrderedDict keyed by hash is used as an insertion-ordered set, so
        # the oldest entry can be evicted in O(1) once the per-session cap is
        # hit. Values are unused.
        now = time.time()
        entry = self._sessions.get(session_id)
        if entry is None or (now - entry["touched"]) > self.ttl_seconds:
            entry = {"hashes": OrderedDict(), "touched": now}
            self._sessions[session_id] = entry
            self._prune()
        entry["touched"] = now
        return entry["hashes"]

    async def is_duplicate(self, session_id: str, doc_hash: str) -> bool:
        return doc_hash in self._get_session(session_id)

    async def add(self, session_id: str, doc_hash: str) -> None:
        hashes = self._get_session(session_id)
        # Re-adding an existing hash must not move it to the newest position:
        # eviction is FIFO by first-seen, not LRU. A block resent on every turn
        # would otherwise pin itself in the cache forever while genuinely
        # distinct blocks got evicted around it.
        if doc_hash in hashes:
            return
        hashes[doc_hash] = None
        while len(hashes) > self.max_hashes_per_session:
            hashes.popitem(last=False)


class RedisHashStore(SessionHashStore):
    """Redis/Valkey-backed store with an in-memory fallback.

    ``client`` is an async redis-py client (e.g. from
    ``byoai.cache.redis.make_redis_client``). On any Redis error the store
    transparently falls back to an :class:`InMemoryHashStore`, so a Redis
    outage degrades dedup quality rather than breaking the request path —
    identical to the proxy's original behavior.

    Every add is mirrored into the fallback unconditionally, so it always
    holds a complete record and an outage needs no recovery, replay, or
    health-tracking machinery — the fallback simply answers instead.

    Mirroring only *during* an outage was tried and reverted: it saves in-process
    memory while Redis is healthy, but it makes correctness depend on detecting
    recovery and replaying state between two stores, and every edge case there
    (partial failures, per-session vs. store-wide health, TTL expiry, concurrent
    writes mid-replay) is a way to silently answer "not a duplicate" for content
    that was already sent. The memory it saved is not worth that: the caps on
    :class:`InMemoryHashStore` already bound the mirror, which is what kept this
    store from growing without limit in the first place.
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
        # still sees what this process recorded. The fallback's own caps bound
        # what this costs.
        await self.fallback.add(session_id, doc_hash)
        key = self._key(session_id)
        try:
            await self.client.sadd(key, doc_hash)
            await self.client.expire(key, self.ttl_seconds)
        except Exception:
            pass

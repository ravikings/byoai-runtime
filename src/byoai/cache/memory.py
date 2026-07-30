"""In-memory cache for development and tests. Same contract as the Redis adapter:

* writes confined to the namespace prefix
* ``ttl`` in seconds; explicit ``ttl=None`` falls back to ``default_ttl``;
  a non-positive TTL means "expire immediately" (nothing stored)
* read-only session reader configured via ``session_reader={"pattern": ...}``
  (``session_data`` simulates the pre-existing application state) — unlike
  :class:`~byoai.cache.redis.RedisCache`, there's no ``"format"`` key: values
  are returned exactly as stored in ``session_data``, with no serialization
  step for a format to apply to
* ``max_size`` bounds memory in long-running processes; oldest entries
  (by insertion/last-write order) are evicted once the cap is hit
"""

from __future__ import annotations

import time
from typing import Any


class MemoryCache:
    def __init__(
        self,
        *,
        namespace: str = "byoai:",
        default_ttl: int | None = 3600,
        session_reader: dict[str, str] | None = None,
        session_data: dict[str, Any] | None = None,
        max_size: int | None = None,
    ) -> None:
        self.namespace = namespace
        self.default_ttl = default_ttl
        self.max_size = max_size
        self._store: dict[str, tuple[Any, float | None]] = {}
        # Simulated "existing app state" for the read-only session reader.
        self._session_data = session_data or {}
        self._session_pattern = (session_reader or {}).get("pattern")

    def _key(self, key: str) -> str:
        return key if key.startswith(self.namespace) else f"{self.namespace}{key}"

    async def get(self, key: str) -> Any | None:
        entry = self._store.get(self._key(key))
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() >= expires_at:
            del self._store[self._key(key)]
            return None
        return value

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        effective_ttl = ttl if ttl is not None else self.default_ttl
        if effective_ttl is not None and effective_ttl <= 0:
            return  # a non-positive TTL means "expire immediately": don't store
        expires_at = time.monotonic() + effective_ttl if effective_ttl is not None else None
        full_key = self._key(key)
        self._store.pop(full_key, None)  # re-insert at the end (most-recent)
        self._store[full_key] = (value, expires_at)
        if self.max_size is not None:
            while len(self._store) > self.max_size:
                self._store.pop(next(iter(self._store)))  # oldest = FIFO head

    async def delete(self, key: str) -> None:
        self._store.pop(self._key(key), None)

    async def read_session(self, **params: str) -> Any | None:
        if not self._session_pattern:
            return None
        return self._session_data.get(self._session_pattern.format(**params))

    async def close(self) -> None:
        self._store.clear()

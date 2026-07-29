"""Redis/Valkey cache adapter with non-invasive state management.

* All writes go under an isolated namespace (default ``byoai:``) — existing
  application keys are never touched.
* ``session_reader`` reads existing application state (chat histories, session
  blobs) read-only through a key-pattern mapping, so ByoAI can reuse what your
  app already stores without migration.

Requires the ``redis`` extra: ``pip install byoai-runtime[redis]``.
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import CacheError, ConfigurationError


class RedisCache:
    def __init__(
        self,
        *,
        url: str = "redis://localhost:6379",
        namespace: str = "byoai:",
        session_reader: dict[str, str] | None = None,
        client: Any | None = None,
        default_ttl: int | None = None,
    ) -> None:
        if client is None:
            try:
                import redis.asyncio as aioredis
            except ImportError as exc:  # pragma: no cover
                raise ConfigurationError(
                    "RedisCache requires the redis package: pip install 'byoai-runtime[redis]'"
                ) from exc
            client = aioredis.from_url(url, decode_responses=True)
        self._client = client
        self.namespace = namespace
        self.default_ttl = default_ttl
        session_reader = session_reader or {}
        self._session_pattern: str | None = session_reader.get("pattern")
        self._session_format: str = session_reader.get("format", "json")

    def _key(self, key: str) -> str:
        return key if key.startswith(self.namespace) else f"{self.namespace}{key}"

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._client.get(self._key(key))
        except Exception as exc:
            raise CacheError(f"redis get failed: {exc}") from exc
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        # Always JSON-encode (strings included) so get() round-trips the exact
        # value and type — set("flag", "true") must come back as the str "true".
        payload = json.dumps(value, default=str)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        if effective_ttl is not None and effective_ttl <= 0:
            return  # a non-positive TTL means "expire immediately": don't store
        try:
            await self._client.set(self._key(key), payload, ex=effective_ttl)
        except Exception as exc:
            raise CacheError(f"redis set failed: {exc}") from exc

    async def delete(self, key: str) -> None:
        try:
            await self._client.delete(self._key(key))
        except Exception as exc:
            raise CacheError(f"redis delete failed: {exc}") from exc

    async def read_session(self, **params: str) -> Any | None:
        """Read existing app state via the configured key pattern. Never writes."""
        if not self._session_pattern:
            return None
        key = self._session_pattern.format(**params)
        try:
            key_type = await self._client.type(key)
            if key_type in ("none", b"none"):
                return None
            if key_type in ("list", b"list"):
                items = await self._client.lrange(key, 0, -1)
                return [self._decode(i) for i in items]
            raw = await self._client.get(key)
        except Exception as exc:
            raise CacheError(f"redis session read failed: {exc}") from exc
        return self._decode(raw)

    def _decode(self, raw: Any) -> Any:
        if raw is None or self._session_format != "json":
            return raw
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except AttributeError:  # older redis-py
            await self._client.close()

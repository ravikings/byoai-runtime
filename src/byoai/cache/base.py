"""Cache store protocol.

Two distinct concerns, deliberately kept in one adapter because they share a
connection to the same existing infrastructure:

* **Runtime cache** (read-write): exact-match response cache, planner state,
  execution artifacts. All writes are confined to an isolated namespace
  (default ``byoai:``) so ByoAI never collides with application keys.
* **Session reader** (read-only): ingest *existing* application state — e.g.
  chat history your app already stores in Redis — via a key-pattern mapping.
  ByoAI never writes through this path.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CacheStore(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def read_session(self, **params: str) -> Any | None:
        """Read existing application state through the configured pattern.

        Params fill the pattern's placeholders, e.g.
        ``read_session(user_id="usr_1")`` with pattern
        ``app:users:{user_id}:chat_history``.
        """
        ...

    async def close(self) -> None: ...

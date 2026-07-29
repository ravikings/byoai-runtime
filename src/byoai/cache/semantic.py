"""Semantic (intent) cache: serve responses for *similar* queries, not just
identical ones.

The exact-match cache short-circuits only byte-identical requests. The
semantic cache embeds the query and matches it against previously answered
queries by cosine similarity — "What are our SLA terms?" can be served from
the cached answer to "Tell me about our enterprise SLAs" without an LLM call.

Economics: one embedding call (~5-50ms, ~$0.00002) replaces one LLM call
(hundreds of ms to seconds, 100-1000× the cost) whenever intent matches.
Tune ``threshold`` to your tolerance — 0.95+ is conservative, below ~0.85
risks serving answers to genuinely different questions.

:class:`MemorySemanticCache` is per-process, numpy-accelerated brute-force
cosine over normalized vectors — exact (not approximate), fast to ~100k
entries. Requires the ``semantic`` extra: ``pip install byoai-runtime[semantic]``.
"""

from __future__ import annotations

import base64
import struct
import time
from typing import Any, Protocol, runtime_checkable

from ..errors import CacheError, ConfigurationError


@runtime_checkable
class SemanticCacheStore(Protocol):
    async def find(
        self, embedding: list[float], *, threshold: float
    ) -> tuple[str, float] | None:
        """Best cached response with cosine similarity >= threshold, as
        ``(response, score)``; None on miss."""
        ...

    async def add(self, embedding: list[float], response: str) -> None: ...

    async def close(self) -> None: ...


class MemorySemanticCache:
    """Ring-buffer semantic cache: fixed ``capacity``, oldest entries evicted.

    Vectors are L2-normalized on insert so similarity is a single matrix-vector
    product. TTL is wall-clock seconds per entry (None = no expiry).
    """

    def __init__(self, *, capacity: int = 10_000, ttl: int | None = 3600) -> None:
        try:
            import numpy
        except ImportError as exc:  # pragma: no cover
            raise ConfigurationError(
                "MemorySemanticCache requires numpy: pip install 'byoai-runtime[semantic]'"
            ) from exc
        self._np = numpy
        self.capacity = capacity
        self.ttl = ttl
        self._matrix: Any = None  # (capacity, dim) float32, rows normalized
        self._responses: list[str | None] = [None] * capacity
        self._expires: Any = numpy.zeros(capacity, dtype=numpy.float64)
        self._next = 0
        self._count = 0

    def _normalize(self, embedding: list[float]) -> Any:
        vector = self._np.asarray(embedding, dtype=self._np.float32)
        norm = float(self._np.linalg.norm(vector))
        if norm == 0.0:
            raise ConfigurationError("cannot cache a zero-magnitude embedding")
        return vector / norm

    async def add(
        self,
        embedding: list[float],
        response: str,
        *,
        expires_at: float | None = None,
    ) -> None:
        """Store an entry. ``expires_at`` (monotonic clock) overrides the
        store's TTL — used by shared/persistent backends replaying entries
        whose remaining lifetime differs from a fresh one."""
        if expires_at is None:
            if self.ttl is not None and self.ttl <= 0:
                return  # a non-positive TTL means "expire immediately"
            expires_at = (
                (time.monotonic() + self.ttl) if self.ttl is not None else float("inf")
            )
        elif expires_at <= time.monotonic():
            return  # already expired
        vector = self._normalize(embedding)
        if self._matrix is None:
            self._matrix = self._np.zeros(
                (self.capacity, vector.shape[0]), dtype=self._np.float32
            )
        slot = self._next
        self._matrix[slot] = vector
        self._responses[slot] = response
        self._expires[slot] = expires_at
        self._next = (self._next + 1) % self.capacity
        self._count = min(self._count + 1, self.capacity)

    async def find(
        self, embedding: list[float], *, threshold: float
    ) -> tuple[str, float] | None:
        if self._count == 0 or self._matrix is None:
            return None
        vector = self._normalize(embedding)
        live = self._matrix[: self._count] @ vector  # cosine, rows pre-normalized
        alive = self._expires[: self._count] > time.monotonic()
        live = self._np.where(alive, live, -1.0)
        best = int(self._np.argmax(live))
        score = float(live[best])
        if score < threshold:
            return None
        response = self._responses[best]
        return (response, score) if response is not None else None

    async def close(self) -> None:
        self._matrix = None
        self._responses = [None] * self.capacity
        self._expires = self._np.zeros(self.capacity, dtype=self._np.float64)
        self._count = 0
        self._next = 0  # keep the write cursor inside the scanned window on reuse


class RedisSemanticCache:
    """Shared, persistent semantic cache on an existing Redis/Valkey.

    Entries live in one Redis Stream under the isolated ``byoai:`` namespace
    (embedding packed as base64 float32 + response + wall-clock expiry).
    Every worker keeps a local numpy mirror and catches up incrementally
    (``XRANGE`` from its last-seen id) before each lookup — usually an empty
    round-trip. So intent hits are shared across processes/replicas and
    survive restarts, while similarity math stays local and fast.

    ``XTRIM MAXLEN ~capacity`` bounds the stream; expiry is enforced at
    lookup time via each entry's wall-clock deadline.

    Requires the ``redis`` and ``semantic`` extras.
    """

    def __init__(
        self,
        *,
        url: str = "redis://localhost:6379",
        stream: str = "byoai:semcache",
        capacity: int = 10_000,
        ttl: int | None = 3600,
        client: Any | None = None,
        mode: str = "standalone",
        sentinels: list | None = None,
        service_name: str | None = None,
    ) -> None:
        if client is None:
            from .redis import make_redis_client

            client = make_redis_client(
                url=url, mode=mode, sentinels=sentinels, service_name=service_name
            )
        self._client = client
        self.stream = stream
        self.capacity = capacity
        self.ttl = ttl
        self._mirror = MemorySemanticCache(capacity=capacity, ttl=ttl)
        self._last_id = "0-0"

    @staticmethod
    def _pack(embedding: list[float]) -> str:
        return base64.b64encode(
            struct.pack(f"<{len(embedding)}f", *embedding)
        ).decode("ascii")

    @staticmethod
    def _unpack(packed: str) -> list[float]:
        raw = base64.b64decode(packed)
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))

    def _wall_to_monotonic(self, expires_at_wall: float) -> float:
        # Cross-worker expiry necessarily rides wall clocks (like Redis EX
        # itself): precision is bounded by NTP sync between hosts. The one-time
        # conversion here freezes the remaining TTL into this process's
        # monotonic clock, so later wall-clock steps can't resurrect or kill
        # entries retroactively.
        if expires_at_wall == float("inf"):
            return float("inf")
        return time.monotonic() + (expires_at_wall - time.time())

    async def _sync(self) -> None:
        """Replay entries other workers appended since our last sync."""
        try:
            entries = await self._client.xrange(
                self.stream, min=f"({self._last_id}", max="+", count=self.capacity
            )
        except Exception as exc:
            raise CacheError(f"semantic cache sync failed: {exc}") from exc
        for entry_id, fields in entries:
            self._last_id = entry_id
            expires_wall = float(fields.get("e", "inf"))
            await self._mirror.add(
                self._unpack(fields["v"]),
                fields["r"],
                expires_at=self._wall_to_monotonic(expires_wall),
            )

    async def add(self, embedding: list[float], response: str) -> None:
        if self.ttl is not None and self.ttl <= 0:
            return
        expires_wall = (time.time() + self.ttl) if self.ttl is not None else float("inf")
        fields = {"v": self._pack(embedding), "r": response, "e": str(expires_wall)}
        try:
            entry_id = await self._client.xadd(
                self.stream, fields, maxlen=self.capacity, approximate=True
            )
        except Exception as exc:
            raise CacheError(f"semantic cache write failed: {exc}") from exc
        # Mirror locally so our own writes are immediately findable without a
        # round-trip; advancing _last_id past our entry avoids re-adding it.
        self._last_id = entry_id
        await self._mirror.add(
            embedding, response, expires_at=self._wall_to_monotonic(expires_wall)
        )

    async def find(
        self, embedding: list[float], *, threshold: float
    ) -> tuple[str, float] | None:
        await self._sync()
        return await self._mirror.find(embedding, threshold=threshold)

    async def close(self) -> None:
        await self._mirror.close()
        try:
            await self._client.aclose()
        except AttributeError:  # older redis-py
            await self._client.close()

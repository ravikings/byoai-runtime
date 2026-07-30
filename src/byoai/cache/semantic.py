"""Semantic (intent) cache: serve responses for *similar* queries, not just
identical ones.

The exact-match cache short-circuits only byte-identical requests. The
semantic cache embeds the query and matches it against previously answered
queries by similarity (cosine by default) — "What are our SLA terms?" can be
served from the cached answer to "Tell me about our enterprise SLAs" without
an LLM call.

Economics: one embedding call (~5-50ms, ~$0.00002) replaces one LLM call
(hundreds of ms to seconds, 100-1000× the cost) whenever intent matches.
Tune ``threshold`` to your tolerance — 0.95+ is conservative, below ~0.85
risks serving answers to genuinely different questions (guidance assumes the
default ``"cosine"`` metric; see ``metric=`` below for other scales).

:class:`MemorySemanticCache` is per-process, numpy-accelerated brute-force
similarity search — exact (not approximate), fast to ~100k entries. The
scoring itself is pluggable via ``metric=``: presets ``"cosine"`` (default),
``"dot"``, ``"euclidean"``, or a bare callable ``(matrix, vector) -> scores``.
Requires the ``semantic`` extra: ``pip install byoai-runtime[semantic]``.
"""

from __future__ import annotations

import asyncio
import base64
import struct
import time
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from .._config_resolve import resolve_preset
from ..errors import CacheError, ConfigurationError

# Below this row count the matrix-vector product is cheaper than a thread
# hop; above it, running inline would stall the event loop for every
# concurrent request on the worker.
_OFFLOAD_MIN_ROWS = 4096

MetricName = Literal["cosine", "dot", "euclidean"]
# (matrix (n, dim), vector (dim,)) -> scores (n,), higher = more similar.
SimilarityFn = Callable[[Any, Any], Any]


def _l2_normalize(vector: Any) -> Any:
    import numpy  # already a hard dependency by the time any metric fn runs

    norm = float(numpy.linalg.norm(vector))
    if norm == 0.0:
        raise ConfigurationError(
            "cannot cache a zero-magnitude embedding under the 'cosine' metric"
        )
    return vector / norm


def _identity(vector: Any) -> Any:
    return vector


def _inner_product(matrix: Any, vector: Any) -> Any:
    return matrix @ vector


def _negative_squared_euclidean(matrix: Any, vector: Any) -> Any:
    diff = matrix - vector
    return -(diff * diff).sum(axis=1)


# name -> (normalize-on-insert/query, score matrix against query vector)
_METRICS: dict[str, tuple[Callable[[Any], Any], SimilarityFn]] = {
    "cosine": (_l2_normalize, _inner_product),
    "dot": (_identity, _inner_product),
    "euclidean": (_identity, _negative_squared_euclidean),
}


@runtime_checkable
class SemanticCacheStore(Protocol):
    async def find(
        self, embedding: list[float], *, threshold: float
    ) -> tuple[str, float] | None:
        """Best cached response scoring >= threshold under the store's
        configured ``metric`` (cosine similarity by default), as
        ``(response, score)``; None on miss."""
        ...

    async def add(self, embedding: list[float], response: str) -> None: ...

    async def close(self) -> None: ...


class MemorySemanticCache:
    """Ring-buffer semantic cache: fixed ``capacity``, oldest entries evicted.

    TTL is wall-clock seconds per entry (None = no expiry). ``metric`` selects
    how a query vector is scored against stored ones — a preset name or a
    bare callable:

    * ``"cosine"`` (default) — cosine similarity, range [-1, 1]. Vectors are
      L2-normalized at insert and query time so a hit is a single
      matrix-vector product. The module docstring's ``threshold`` guidance
      (0.85-0.95+) assumes this metric.
    * ``"dot"`` — raw inner product, no normalization. Useful when an
      embedding model's vector magnitude is itself meaningful.
    * ``"euclidean"`` — negative squared Euclidean distance (higher = closer,
      unbounded range), no normalization.
    * a bare callable ``(matrix, vector) -> scores``, one score per stored
      row, higher = more similar — receives raw (non-normalized) vectors.
      Whatever range your callable produces is the range ``threshold`` gets
      compared against.
    """

    def __init__(
        self,
        *,
        capacity: int = 10_000,
        ttl: int | None = 3600,
        metric: MetricName | SimilarityFn = "cosine",
    ) -> None:
        try:
            import numpy
        except ImportError as exc:  # pragma: no cover
            raise ConfigurationError(
                "MemorySemanticCache requires numpy: pip install 'byoai-runtime[semantic]'"
            ) from exc
        self._np = numpy
        self.capacity = capacity
        self.ttl = ttl
        self.metric = metric
        if callable(metric):
            self._normalize_vector: Callable[[Any], Any] = _identity
            self._score: SimilarityFn = metric
        else:
            self._normalize_vector, self._score = resolve_preset(
                metric,
                _METRICS,
                kind="metric",
                callable_signature="(matrix, vector) -> scores",
            )
        self._matrix: Any = None  # (capacity, dim) float32, rows normalized
        self._responses: list[str | None] = [None] * capacity
        self._expires: Any = numpy.zeros(capacity, dtype=numpy.float64)
        self._next = 0
        self._count = 0
        # Serializes writers against lookups: large lookups run in a worker
        # thread, and an add() interleaving with one could tear a row mid-read.
        self._mutex = asyncio.Lock()

    def _normalize(self, embedding: list[float]) -> Any:
        vector = self._np.asarray(embedding, dtype=self._np.float32)
        return self._normalize_vector(vector)

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
        async with self._mutex:
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
            return None  # fast path: skip normalizing/locking for an empty cache
        vector = self._normalize(embedding)
        async with self._mutex:
            # Re-checked under the lock: a concurrent close() queued behind an
            # in-flight offloaded find() could have cleared the state between
            # the fast-path check above and acquiring the lock here.
            if self._count == 0 or self._matrix is None:
                return None
            if self._count >= _OFFLOAD_MIN_ROWS:
                return await asyncio.to_thread(self._find_best, vector, threshold)
            return self._find_best(vector, threshold)

    def _find_best(self, vector: Any, threshold: float) -> tuple[str, float] | None:
        live = self._score(self._matrix[: self._count], vector)
        alive = self._expires[: self._count] > time.monotonic()
        # -inf (not a metric-specific sentinel like cosine's -1.0) so a dead
        # row can never outscore a live one under an unbounded metric such as
        # "euclidean" or a custom callable. NaN is masked out the same way —
        # a custom metric callable that divides by zero or underflows must
        # not let NaN win argmax and get served as a bogus cache hit.
        live = self._np.where(alive & ~self._np.isnan(live), live, -self._np.inf)
        best = int(self._np.argmax(live))
        score = float(live[best])
        if not score >= threshold:  # rejects NaN too, unlike `score < threshold`
            return None
        response = self._responses[best]
        return (response, score) if response is not None else None

    async def close(self) -> None:
        # Under the mutex: an offloaded find() releases the event loop while
        # its worker thread reads the matrix, and a concurrent close() tearing
        # the state down mid-read would crash that in-flight lookup.
        async with self._mutex:
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
    lookup time via each entry's wall-clock deadline. ``metric`` selects the
    similarity scoring — same presets/callable escape hatch as
    :class:`MemorySemanticCache`, which does the actual scoring here.

    Requires the ``redis`` and ``semantic`` extras.
    """

    def __init__(
        self,
        *,
        url: str = "redis://localhost:6379",
        stream: str = "byoai:semcache",
        capacity: int = 10_000,
        ttl: int | None = 3600,
        metric: MetricName | SimilarityFn = "cosine",
        client: Any | None = None,
        mode: str = "standalone",
        sentinels: list | None = None,
        service_name: str | None = None,
        approximate_trim: bool = True,
        **client_kwargs: Any,
    ) -> None:
        if client is None:
            from .redis import make_redis_client

            client = make_redis_client(
                url=url, mode=mode, sentinels=sentinels, service_name=service_name,
                **client_kwargs,
            )
        # Explicitly typed: see the matching comment in cache/redis.py.
        self._client: Any = client
        self.stream = stream
        # False = exact XTRIM MAXLEN (costlier) instead of "~" approximate
        # trimming — for deployments that need an exact capacity bound.
        self.approximate_trim = approximate_trim
        self.capacity = capacity
        self.ttl = ttl
        self.metric = metric
        self._mirror = MemorySemanticCache(capacity=capacity, ttl=ttl, metric=metric)
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
                self.stream, fields, maxlen=self.capacity, approximate=self.approximate_trim
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

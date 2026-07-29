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

import time
from typing import Any, Protocol, runtime_checkable

from ..errors import ConfigurationError


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

    async def add(self, embedding: list[float], response: str) -> None:
        if self.ttl is not None and self.ttl <= 0:
            return  # a non-positive TTL means "expire immediately": don't store
        vector = self._normalize(embedding)
        if self._matrix is None:
            self._matrix = self._np.zeros(
                (self.capacity, vector.shape[0]), dtype=self._np.float32
            )
        slot = self._next
        self._matrix[slot] = vector
        self._responses[slot] = response
        self._expires[slot] = (
            (time.monotonic() + self.ttl) if self.ttl is not None else float("inf")
        )
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

"""Vector store protocol with zero-migration schema mapping.

Adapters query *existing* vector tables/collections directly. A ``schema_map``
tells the adapter which existing columns/fields hold each logical slot::

    schema_map = {
        "id": "doc_id",
        "embedding": "embedding_v2",
        "content": "raw_text",
        "metadata": "payload_json",
    }

No migrations, no re-indexing, no table duplication.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from ..types import Document

DEFAULT_SCHEMA_MAP = {
    "id": "id",
    "embedding": "embedding",
    "content": "content",
    "metadata": "metadata",
}


@runtime_checkable
class VectorStore(Protocol):
    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]: ...

    async def close(self) -> None: ...


class FunctionVectorStore:
    """Adapts a bare async search function into a :class:`VectorStore` — for
    a custom retrieval backend that doesn't fit a declarative
    ``vector_store={...}`` config. ``search()`` is the only operation this
    protocol has, so one function is the whole adapter — no class needed:

        async def my_search(embedding: list[float], *, top_k=5, filters=None) -> list[Document]:
            ...

        Runtime(vector_store=my_search)  # auto-wrapped
    """

    def __init__(
        self,
        fn: Callable[..., Awaitable[list[Document]]],
        *,
        name: str | None = None,
    ) -> None:
        self._fn = fn
        self.name = name or getattr(fn, "__name__", "function_vector_store")

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]:
        from ..errors import VectorStoreError

        try:
            result = await self._fn(embedding, top_k=top_k, filters=filters)
        except VectorStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - every adapter failure must be a
            # typed ByoAIError, same as FunctionProvider wraps a bare function's failures.
            raise VectorStoreError(f"{self.name}: search failed: {exc}") from exc
        if not isinstance(result, list):
            # A wrapped function that returns None (e.g. a branch falling through
            # without an explicit return) must not propagate as `ctx.documents = None`
            # and crash downstream call sites that assume a list.
            raise VectorStoreError(
                f"{self.name}: expected a list[Document], got {type(result).__name__}"
            )
        return result

    async def close(self) -> None:
        pass  # the wrapped function owns whatever client/resources it uses

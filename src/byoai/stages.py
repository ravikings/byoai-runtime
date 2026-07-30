"""Built-in pipeline stages.

These implement the default execution path (normalize input → cache lookup →
provider call). Applications can reorder, remove, replace, or interleave their
own stages freely — the runtime does not special-case any of them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from . import _json as json
from . import events as ev
from .cache.base import CacheStore
from .context import RequestContext
from .errors import CacheError, ConfigurationError
from .events import EventBus
from .providers.router import ProviderRouter
from .types import Document, Message
from .vector.base import VectorStore

# ctx.state keys used by the built-in stages (namespaced to avoid collisions).
STATE_STREAMING = "byoai.streaming"
STATE_CACHE_KEY = "byoai.cache_key"
STATE_SEMANTIC_EMBEDDING = "byoai.semantic_embedding"

# Single source of truth for the semantic-cache similarity floor; runtime.py
# reads this default too instead of duplicating the literal.
DEFAULT_SEMANTIC_THRESHOLD = 0.92


class ContextResolver:
    """Normalize ``ctx.input`` into ``ctx.messages``.

    Accepts:
      * ``str`` — becomes a single user message
      * ``{"messages": [{"role": ..., "content": ...}, ...]}``
      * ``{"query"|"input"|"prompt": str, ...}``
      * ``list[Message]`` / list of role-content dicts

    Optionally prepends a system prompt and existing session history read
    (read-only) from the cache adapter's session reader.
    """

    name = "context_resolver"

    def __init__(
        self,
        *,
        system_prompt: str | None = None,
        cache: CacheStore | None = None,
        session_params: Callable[[RequestContext], dict[str, str]] | None = None,
        max_history_messages: int = 20,
    ) -> None:
        self.system_prompt = system_prompt
        self.cache = cache
        self.session_params = session_params
        self.max_history_messages = max_history_messages

    async def execute(self, ctx: RequestContext) -> None:
        messages: list[Message] = []
        # ctx.system_prompt is a per-call override (None = not overridden,
        # "" = explicitly cleared for this call) set by Runtime.execute().
        prompt = ctx.system_prompt if ctx.system_prompt is not None else self.system_prompt
        if prompt:
            messages.append(Message(role="system", content=prompt))
        messages.extend(await self._history(ctx))
        messages.extend(self._normalize(ctx.input))
        ctx.messages = messages

    async def _history(self, ctx: RequestContext) -> list[Message]:
        if self.cache is None or self.max_history_messages <= 0:
            return []
        params: dict[str, str] = {}
        if self.session_params is not None:
            params = self.session_params(ctx)
        elif ctx.user_id:
            params = {"user_id": ctx.user_id}
        elif ctx.session_id:
            params = {"session_id": ctx.session_id}
        if not params:
            return []
        try:
            history = await self.cache.read_session(**params)
        except (CacheError, KeyError):
            return []
        if not isinstance(history, list):
            return []
        out = [m for m in (_coerce_message(item) for item in history) if m is not None]
        return out[-self.max_history_messages :]

    def _normalize(self, raw: Any) -> list[Message]:
        if isinstance(raw, str):
            return [Message(role="user", content=raw)]
        if isinstance(raw, Message):
            return [raw]
        if isinstance(raw, list):
            out = [m for m in (_coerce_message(item) for item in raw) if m is not None]
            if out:
                return out
        if isinstance(raw, dict):
            if isinstance(raw.get("messages"), list):
                out = [
                    m for m in (_coerce_message(item) for item in raw["messages"]) if m is not None
                ]
                if out:
                    return out
            for key in ("query", "input", "prompt"):
                if isinstance(raw.get(key), str):
                    return [Message(role="user", content=raw[key])]
        raise ConfigurationError(
            f"cannot normalize input of type {type(raw).__name__} into messages"
        )


def _coerce_message(item: Any) -> Message | None:
    if isinstance(item, Message):
        return item
    if isinstance(item, dict) and "content" in item:
        role = item.get("role", "user")
        if role in ("system", "user", "assistant", "tool"):
            content = item["content"]
            # str/list pass through untouched (list = provider content
            # blocks, e.g. Anthropic tool_use/tool_result); anything else
            # (a stray int/bool from a hand-written history dict) is
            # stringified rather than rejected.
            if not isinstance(content, (str, list)):
                content = str(content)
            return Message(role=role, content=content)
    return None


class CacheLookup:
    """Exact-match response cache. Short-circuits the pipeline on a hit.

    The cache key fingerprints the normalized messages, model, pipeline,
    provider options (temperature, top_p, ...) and retrieval filters, so
    requests that differ only in those fields never collide on the same
    entry. The runtime writes the response back (with its configured
    ``cache_ttl``) after a successful non-streamed, non-cached execution.
    """

    name = "cache_lookup"

    def __init__(
        self,
        cache: CacheStore,
        *,
        bus: EventBus | None = None,
        extra_fingerprint: Callable[[RequestContext], Any] | None = None,
    ) -> None:
        self.cache = cache
        self._bus = bus
        # Hook for apps needing extra key dimensions (e.g. a tenant id from
        # ctx.state) without subclassing this stage.
        self.extra_fingerprint = extra_fingerprint

    def fingerprint(self, ctx: RequestContext) -> str:
        basis: dict[str, Any] = {
            "messages": [m.to_dict() for m in ctx.messages],
            "model": ctx.model,
            "pipeline": ctx.pipeline_name,
            "provider_options": ctx.state.get("provider_options"),
            "filters": ctx.state.get("filters"),
        }
        if self.extra_fingerprint is not None:
            basis["extra"] = self.extra_fingerprint(ctx)
        encoded = json.dumps(basis, sort_keys=True, default=str)
        return "cache:" + hashlib.sha256(encoded.encode()).hexdigest()

    async def execute(self, ctx: RequestContext) -> None:
        if ctx.state.get(STATE_STREAMING):
            return  # streamed responses are not served from the exact-match cache
        try:
            key = self.fingerprint(ctx)
        except Exception:
            # A caller-supplied extra_fingerprint hook can raise (e.g. a
            # missing ctx.state key); a broken key computation must degrade
            # to "skip caching for this request", same as any other cache
            # failure — never fail a request the provider could have answered.
            return
        ctx.state[STATE_CACHE_KEY] = key
        try:
            hit = await self.cache.get(key)
        except CacheError:
            hit = None  # cache outage must never fail the request
        if hit is not None:
            if self._bus:
                await self._bus.emit(ev.CACHE_HIT, ctx=ctx, key=key)
            # Entries are {"content", "model", "provider", "finish_reason"}
            # dicts; tolerate bare strings so pre-existing/hand-written
            # entries still work.
            if isinstance(hit, dict) and "content" in hit:
                ctx.model = hit.get("model") or ctx.model
                ctx.provider = hit.get("provider")
                ctx.finish_reason = hit.get("finish_reason")
                ctx.short_circuit(str(hit["content"]), cached=True)
            else:
                ctx.short_circuit(str(hit), cached=True)
        elif self._bus:
            await self._bus.emit(ev.CACHE_MISS, ctx=ctx, key=key)


Embedder = Callable[[str], Awaitable[list[float]]]


def _last_user_message(ctx: RequestContext) -> str | None:
    """Text of the last user message, for the Embedder (semantic cache /
    vector retrieval), which is str-only. A list-valued content (e.g. a
    tool_result turn) is reduced to its text blocks; ``None`` if it has none
    (a pure tool_result turn) so callers degrade to a no-op instead of
    feeding the embedder a non-str value.
    """
    content = next((m.content for m in reversed(ctx.messages) if m.role == "user"), None)
    if content is None or isinstance(content, str):
        return content
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    return text or None


class SemanticCacheLookup:
    """Intent cache: short-circuit when a *similar* (not identical) query was
    already answered. Runs after the exact-match cache — exact hits are cheaper
    (no embedding call). On a miss, the query embedding is kept on the context
    so the runtime can store the eventual response for future intent hits.

    Streaming requests participate too: a hit streams back as a single chunk.
    """

    name = "semantic_cache_lookup"

    def __init__(
        self,
        store: Any,  # SemanticCacheStore
        embedder: Embedder,
        *,
        threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
        bus: EventBus | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.threshold = threshold
        self._bus = bus

    async def execute(self, ctx: RequestContext) -> None:
        query = _last_user_message(ctx)
        if query is None:
            return
        # A semantic-cache or embedder hiccup must never fail a request that
        # the provider would have answered — degrade to a miss instead. Catch
        # broadly, not just ByoAIError: embedder= accepts any user-supplied
        # `async (str) -> list[float]` callable, which won't necessarily raise
        # our typed errors (e.g. a raw httpx.ConnectError or ConnectionError).
        try:
            embedding = await self.embedder(query)
            ctx.state[STATE_SEMANTIC_EMBEDDING] = embedding
            hit = await self.store.find(embedding, threshold=self.threshold)
        except Exception as exc:  # noqa: BLE001 - degrade-to-miss must be unconditional
            ctx.state.pop(STATE_SEMANTIC_EMBEDDING, None)  # skip write-back too
            if self._bus:
                await self._bus.emit(ev.CACHE_MISS, ctx=ctx, semantic=True, error=str(exc))
            return
        if hit is not None:
            response, score = hit
            if self._bus:
                await self._bus.emit(ev.CACHE_HIT, ctx=ctx, semantic=True, score=score)
            ctx.metadata["semantic_cache_score"] = score
            ctx.short_circuit(response, cached=True)
        elif self._bus:
            await self._bus.emit(ev.CACHE_MISS, ctx=ctx, semantic=True)


class VectorRetrieve:
    """Retrieve documents from an existing vector store for the last user message.

    ``embedder`` is supplied by the application (it owns embedding model choice);
    ByoAI executes the retrieval. Filters come from ``ctx.state['filters']`` or
    the constructor default, in the unified AST dialect.
    """

    name = "vector_retrieve"

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
        bus: EventBus | None = None,
        format_document: Callable[[Document], str] | None = None,
        context_header: str = "Relevant context retrieved for this request:",
        insert_at: Callable[[RequestContext], int] | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.top_k = top_k
        self.filters = filters
        self._bus = bus
        # Customize the RAG prompt wrapper (citation format, instructions to
        # the model) without subclassing this stage.
        self.format_document = format_document or (lambda d: f"[{d.id}] {d.content}")
        self.context_header = context_header
        # Default: just before the last message. Override to e.g. always
        # append at the end, or merge into an existing system message.
        self.insert_at = insert_at or (lambda ctx: max(len(ctx.messages) - 1, 0))

    async def execute(self, ctx: RequestContext) -> None:
        query = _last_user_message(ctx)
        if query is None:
            return
        embedding = await self.embedder(query)
        filters = ctx.state.get("filters", self.filters)
        documents: list[Document] = await self.store.search(
            embedding, top_k=self.top_k, filters=filters
        )
        ctx.documents = documents
        if self._bus:
            await self._bus.emit(ev.VECTOR_RETRIEVED, ctx=ctx, count=len(documents))
        if documents:
            context_block = "\n\n".join(self.format_document(d) for d in documents)
            ctx.messages.insert(
                self.insert_at(ctx),
                Message(role="system", content=f"{self.context_header}\n\n{context_block}"),
            )


class ProviderCall:
    """Terminal stage: call the provider router with the built messages.

    In streaming mode (``ctx.state[STATE_STREAMING]``) this stage is a no-op —
    the runtime streams from the router itself after the pipeline finishes
    preparing the context.
    """

    name = "provider_call"

    def __init__(self, router: ProviderRouter, **default_options: Any) -> None:
        self.router = router
        self.default_options = default_options

    async def execute(self, ctx: RequestContext) -> None:
        if ctx.state.get(STATE_STREAMING):
            return
        options = {**self.default_options, **ctx.state.get("provider_options", {})}
        if ctx.model:
            options["model"] = ctx.model
        response = await self.router.complete(ctx.messages, **options)
        ctx.response = response.content
        ctx.model = response.model
        ctx.provider = response.provider
        ctx.usage.add(response.usage)
        ctx.finish_reason = response.finish_reason
        ctx.raw_response = response.raw

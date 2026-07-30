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
        history, history_trailing_drop = await self._history(ctx)
        messages.extend(history)
        # The seam between history and this call's own input is the one
        # place a merge can be needed *across* two independently-built
        # lists: history's last message can be a drop survivor (see
        # _history()) with nothing left in history itself to merge into —
        # exactly what history_trailing_drop signals. Every other
        # adjacency (within history, within input) is already resolved by
        # _coerce_messages() itself, at the exact point a drop happens —
        # not here, and not unconditionally: two same-role messages with
        # no drop between them (e.g. a caller directly supplying
        # ctx.input=[Message(user, name="a"), Message(user, name="b")], a
        # documented, supported input shape) are left alone rather than
        # silently merged just because they happen to share a role.
        _append_messages(messages, self._normalize(ctx.input), pending_merge=history_trailing_drop)
        ctx.messages = messages

    async def _history(self, ctx: RequestContext) -> tuple[list[Message], bool]:
        """Returns ``(messages, trailing_drop)`` — ``trailing_drop`` is
        True when the last returned message is a tool-call-turn drop
        survivor with nothing left in this history to merge into (see
        ``execute()``, which is the one place that needs it)."""
        if self.cache is None or self.max_history_messages <= 0:
            return [], False
        params: dict[str, str] = {}
        if self.session_params is not None:
            params = self.session_params(ctx)
        elif ctx.user_id:
            params = {"user_id": ctx.user_id}
        elif ctx.session_id:
            params = {"session_id": ctx.session_id}
        if not params:
            return [], False
        try:
            history = await self.cache.read_session(**params)
        except (CacheError, KeyError):
            return [], False
        if not isinstance(history, list):
            return [], False
        out, out_trailing_drop = _coerce_messages(history)
        # Slicing only ever removes from the *front* (same for
        # _drop_until_valid_start below), so the last coerced message —
        # and thus whether it's a drop survivor — is unaffected by either.
        sliced = _drop_until_valid_start(out[-self.max_history_messages :])
        trailing_drop = bool(sliced) and out_trailing_drop
        return sliced, trailing_drop

    def _normalize(self, raw: Any) -> list[Message]:
        if isinstance(raw, str):
            return [Message(role="user", content=raw)]
        if isinstance(raw, Message):
            return [raw]
        if isinstance(raw, list):
            out, _ = _coerce_messages(raw)
            if out:
                return out
        if isinstance(raw, dict):
            if isinstance(raw.get("messages"), list):
                out, _ = _coerce_messages(raw["messages"])
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
        # Same drop as the dict branch below, for a caller that hands us
        # an already-constructed Message instead of a role/content dict
        # (e.g. reconstructing history as Message objects directly) — a
        # tool-call-only turn's content=None must not sail through
        # unchecked just because it skipped the dict-coercion path.
        if item.content is None and item.role == "assistant":
            return None
        return item
    if isinstance(item, dict) and "content" in item:
        role = item.get("role", "user")
        if role in ("system", "user", "assistant", "tool"):
            content = item["content"]
            # None on an assistant turn (the standard shape for a
            # tool-call-only turn in stored OpenAI/Anthropic history) drops
            # the message entirely rather than sending it. Turning it into
            # the literal text "None" would fabricate something the model
            # never said; turning it into "" isn't safe either —
            # Anthropic's API rejects any non-final message with empty
            # content outright, failing the whole request. The tool call
            # itself isn't representable here anyway (this dict shape only
            # carries role/content), so a turn with no visible text has
            # nothing worth keeping. See _coerce_messages() for why the
            # "tool" replies to a dropped turn like this one also need to
            # go — that requires list-level context this function doesn't
            # have.
            if content is None and role == "assistant":
                return None
            if not isinstance(content, (str, list)):
                content = str(content)
            return Message(role=role, content=content)
    return None


def _item_role(item: Any) -> Any:
    """The role of a raw history/input item, whichever of the two shapes
    ``_coerce_message`` accepts it's in — a plain ``Message`` or a
    role/content dict — so callers that need to look ahead/behind at
    surrounding items (``_coerce_messages``'s "tool" reply skip below,
    ``_is_dropped_tool_call_turn``) don't need to duplicate that check.
    """
    if isinstance(item, Message):
        return item.role
    if isinstance(item, dict):
        return item.get("role", "user")
    return None


def _is_dropped_tool_call_turn(item: Any) -> bool:
    if isinstance(item, Message):
        return item.role == "assistant" and item.content is None
    # "content" in item, not just item.get("content") is None: a dict with
    # no "content" key at all (a malformed/legacy entry _coerce_message
    # already drops on its own, via the same "content" in item guard) must
    # not be mistaken for a tool-call-only turn — that would additionally,
    # incorrectly, delete a legitimate "tool" reply immediately following
    # it.
    return (
        isinstance(item, dict)
        and item.get("role", "user") == "assistant"
        and "content" in item
        and item.get("content") is None
    )


def _merge_content(
    a: str | list[dict[str, Any]], b: str | list[dict[str, Any]]
) -> str | list[dict[str, Any]]:
    if isinstance(a, str) and isinstance(b, str):
        return f"{a}\n\n{b}"
    a_blocks = a if isinstance(a, list) else [{"type": "text", "text": a}]
    b_blocks = b if isinstance(b, list) else [{"type": "text", "text": b}]
    return [*a_blocks, *b_blocks]


def _merge_name(a: str | None, b: str | None) -> str | None:
    # Unlike content, two disagreeing names can't be concatenated into
    # something meaningful — keep it only when both sides actually agree
    # (including both being unset); otherwise drop it rather than
    # arbitrarily keeping one side's value over the other's.
    return a if a == b else None


def _merge_metadata(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    # metadata is app-defined bookkeeping never sent to a provider (see
    # Message.to_dict()) — a plain dict merge (b's keys win on conflict)
    # keeps everything from both sides instead of the all-or-nothing
    # choice content/name require. `Message.metadata`'s type is
    # `dict[str, Any]` with a default_factory of `dict`, but nothing
    # actually stops a caller from constructing one with `metadata=None`
    # explicitly — treat that the same as an empty dict rather than
    # crashing on `{**None, ...}`.
    return {**(a or {}), **(b or {})}


def _merge_messages(a: Message, b: Message) -> Message:
    return Message(
        role=a.role,
        content=_merge_content(a.content, b.content),
        name=_merge_name(a.name, b.name),
        metadata=_merge_metadata(a.metadata, b.metadata),
    )


def _append_messages(dest: list[Message], new: list[Message], *, pending_merge: bool) -> None:
    """Append ``new`` onto ``dest``, merging the seam into one message
    instead of two when ``pending_merge`` says the seam needs it — see
    ``_coerce_messages()`` (within one list) and ``ContextResolver.
    execute()`` (across the history/input boundary) for the two call
    sites and what makes each one's seam eligible. Never merges just
    because two adjacent messages happen to share a role: unlike the
    dropped-turn case this exists for, that's a common, legitimate shape
    (e.g. a caller-supplied ``ctx.input`` list with two intentionally
    distinct same-role turns) that must be left alone, not silently
    collapsed into one.
    """
    if (
        pending_merge
        and dest
        and new
        and dest[-1].role == new[0].role
        and dest[-1].role in ("user", "assistant")
    ):
        dest[-1] = _merge_messages(dest[-1], new[0])
        dest.extend(new[1:])
    else:
        dest.extend(new)


def _coerce_messages(items: list[Any]) -> tuple[list[Message], bool]:
    """Map ``_coerce_message`` over ``items``, but list-aware in two ways:

    * Dropping an assistant tool-call-only turn (``content: None``) also
      drops the "tool" messages immediately following it. Those are that
      turn's tool_result replies — every provider requires a tool_result
      to immediately follow the assistant tool_use turn it responds to,
      so once that turn is gone, keeping its replies would send a still-
      invalid request, just failing on the next message instead of this
      one.
    * If the message immediately before a dropped turn and the message
      immediately after it (once its "tool" replies are skipped too)
      share a role, they're merged into one instead of left as two
      consecutive same-role messages — which a provider enforcing strict
      user/assistant alternation (e.g. Anthropic) rejects outright. This
      merge is scoped to exactly this seam (see ``_append_messages()``):
      it never touches an adjacency that isn't immediately downstream of
      an actual drop, so a caller's own intentionally-adjacent same-role
      messages elsewhere in ``items`` are left untouched.

    Returns ``(messages, trailing_drop)`` — ``trailing_drop`` is True when
    the loop ends still "owing" a merge (the tail of ``items`` was a
    dropped turn, or its skipped "tool" replies, with no further message
    in ``items`` to merge into). Computed here, as a direct byproduct of
    the same single pass that decides every *other* merge in this list,
    rather than by a second, separate scan over ``items`` with its own,
    easy-to-drift-out-of-sync idea of what counts as "skippable" — see
    ``_history()``, the one caller that uses this flag, for why it's
    needed at all (the one merge seam this function can't resolve on its
    own: this list's last message against whatever a caller appends right
    after it).
    """
    out: list[Message] = []
    skip_tool_replies = False
    pending_merge = False
    for item in items:
        if skip_tool_replies:
            if _item_role(item) == "tool":
                continue
            skip_tool_replies = False
        if _is_dropped_tool_call_turn(item):
            skip_tool_replies = True
            pending_merge = True
            continue
        message = _coerce_message(item)
        if message is None:
            continue
        _append_messages(out, [message], pending_merge=pending_merge)
        pending_merge = False
    return out, pending_merge


def _drop_until_valid_start(messages: list[Message]) -> list[Message]:
    """Drop messages from the front of a truncated history slice until
    the first remaining message has role ``"user"`` (or the list is
    exhausted).

    ``_history()`` truncates coerced history to its last
    ``max_history_messages`` entries *after* ``_coerce_messages()`` has
    already run — so a fixed-size window over an alternating conversation
    can start on any role, not just cut a tool_use/tool_result pair (the
    narrowest case): a plain, otherwise-unremarkable alternating history
    can just as easily be windowed to start on "assistant". Every
    provider enforcing strict alternation (e.g. Anthropic requires the
    first message to be role="user") rejects anything else at the front,
    so both "tool" and "assistant" need dropping here, not just "tool".
    """
    start = 0
    while start < len(messages) and messages[start].role != "user":
        start += 1
    return messages[start:]


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
        # provider_options["metadata"] is Runtime.execute()'s provider_metadata=
        # (e.g. {"user_id": ...} for audit correlation) — forwarded into the
        # provider payload but never affects what answer comes back, so it must
        # not be part of the key: an app tagging every call with a per-request
        # id would otherwise defeat exact-match caching entirely.
        provider_options = ctx.state.get("provider_options")
        if isinstance(provider_options, dict) and "metadata" in provider_options:
            provider_options = {k: v for k, v in provider_options.items() if k != "metadata"}
        basis: dict[str, Any] = {
            "messages": [m.to_dict() for m in ctx.messages],
            "model": ctx.model,
            "pipeline": ctx.pipeline_name,
            "provider_options": provider_options,
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

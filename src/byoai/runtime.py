"""The ByoAI Runtime.

Owns execution: middleware, pipelines, events, provider routing, caching and
usage accounting. Owns nothing about *what* the pipeline does — applications
and frameworks compose stages; the runtime executes them.

    runtime = Runtime(llm={"provider": "openai", "model": "gpt-4o"})
    result = await runtime.execute("What are our SLA terms?")
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

from . import events as ev
from .cache.base import CacheStore
from .cache.semantic import SemanticCacheStore
from .config import (
    build_cache,
    build_embedder,
    build_router,
    build_semantic_cache,
    build_vector_store,
    configure_telemetry,
)
from .context import RequestContext
from .errors import ByoAIError, CacheError, ConfigurationError, PipelineNotFoundError
from .events import EventBus, EventHandler
from .middleware import MiddlewareChain, MiddlewareLike
from .pipeline import Pipeline
from .providers.base import LLMProvider
from .providers.router import ProviderRouter, RetryPolicy, SelectionFn, SelectionName
from .stages import (
    DEFAULT_SEMANTIC_THRESHOLD,
    STATE_CACHE_KEY,
    STATE_SEMANTIC_EMBEDDING,
    STATE_STREAMING,
    CacheLookup,
    ContextResolver,
    Embedder,
    ProviderCall,
    SemanticCacheLookup,
)
from .types import ExecutionResult, StreamChunk
from .vector.base import FunctionVectorStore, VectorStore

logger = logging.getLogger(__name__)


class Runtime:
    """The execution engine: owns middleware, pipelines, events, provider
    routing, caching and usage accounting. Configure it declaratively
    (``llm={...}``, ``cache={...}``) or with pre-built adapter instances;
    both styles compose.
    """

    def __init__(
        self,
        *,
        llm: dict[str, Any] | None = None,
        providers: list[LLMProvider | Callable[..., Any]] | None = None,
        cache: dict[str, Any] | CacheStore | None = None,
        vector_store: dict[str, Any] | VectorStore | Callable[..., Any] | None = None,
        semantic_cache: dict[str, Any] | SemanticCacheStore | None = None,
        embedder: dict[str, Any] | Embedder | None = None,
        retry_policy: RetryPolicy | None = None,
        selection: SelectionName | SelectionFn = "ordered",
        system_prompt: str | None = None,
        telemetry: Any | None = None,
    ) -> None:
        self.events = EventBus()
        self.middleware = MiddlewareChain()
        self._pipelines: dict[str, Pipeline] = {}

        self.cache: CacheStore | None = (
            build_cache(cache) if isinstance(cache, dict) else cache
        )
        self.vector_store: VectorStore | None = (
            build_vector_store(vector_store)
            if isinstance(vector_store, dict)
            else FunctionVectorStore(vector_store)  # type: ignore[arg-type]
            if vector_store is not None and not hasattr(vector_store, "search")
            else vector_store
        )

        resolved_providers = list(providers or [])
        if llm is not None:
            resolved_providers = build_router(llm) + resolved_providers
        self.router: ProviderRouter | None = (
            ProviderRouter(
                resolved_providers,
                retry_policy=retry_policy,
                selection=selection,
                event_bus=self.events,
            )
            if resolved_providers
            else None
        )

        self.embedder: Embedder | None = (
            build_embedder(embedder) if isinstance(embedder, dict) else embedder
        )
        self.semantic_cache: SemanticCacheStore | None = (
            build_semantic_cache(semantic_cache)
            if isinstance(semantic_cache, dict)
            else semantic_cache
        )
        if (
            isinstance(semantic_cache, dict)
            # Read the metric the store actually resolved to, not the raw
            # config dict re-defaulted to "cosine" here too: a plugin-
            # provided semantic cache (byoai.semantic_caches entry point, or
            # any future built-in) can default to a non-cosine metric on its
            # own even when the config dict never mentions "metric" at all —
            # re-deriving "cosine" from the dict's absence would silently
            # skip this guard for exactly the case it exists to catch.
            and getattr(self.semantic_cache, "metric", "cosine") != "cosine"
            and "threshold" not in semantic_cache
        ):
            # DEFAULT_SEMANTIC_THRESHOLD (0.92) is calibrated for cosine's
            # [-1, 1] range. Falling back to it silently for e.g. "euclidean"
            # (whose scores are <= 0) would make every lookup miss forever —
            # a working feature going silently inert, not a loud failure.
            raise ConfigurationError(
                f"semantic_cache resolved to metric="
                f"{getattr(self.semantic_cache, 'metric', None)!r} but no explicit "
                "threshold= — the default (0.92) is calibrated for cosine similarity "
                "and won't make sense for this metric's score range"
            )
        self._semantic_threshold = (
            semantic_cache.get("threshold", DEFAULT_SEMANTIC_THRESHOLD)
            if isinstance(semantic_cache, dict)
            else DEFAULT_SEMANTIC_THRESHOLD
        )

        # Default pipeline: resolve context → exact cache → semantic (intent)
        # cache → provider call.
        self.pipeline = Pipeline("default")
        self.pipeline.add(
            ContextResolver(system_prompt=system_prompt, cache=self.cache)
        )
        if self.cache is not None:
            self.pipeline.add(CacheLookup(self.cache, bus=self.events))
        if self.semantic_cache is not None:
            if self.embedder is None:
                raise ConfigurationError(
                    "semantic_cache requires an embedder= (config dict or async callable)"
                )
            self.pipeline.add(
                SemanticCacheLookup(
                    self.semantic_cache,
                    self.embedder,
                    threshold=self._semantic_threshold,
                    bus=self.events,
                )
            )
        if self.router is not None:
            self.pipeline.add(ProviderCall(self.router))
        self._pipelines["default"] = self.pipeline

        # A provider configure_telemetry created for us is ours to shut down
        # (flushing the final span batch); one the caller passed in is theirs.
        self._owned_tracer_provider: Any | None = (
            configure_telemetry(self, telemetry) if telemetry is not None else None
        )

    # -- composition ----------------------------------------------------------

    def use(self, middleware: MiddlewareLike) -> Runtime:
        """Add a middleware wrapping every execution. Chainable."""
        self.middleware.add(middleware)
        return self

    def on(self, event: str, handler: EventHandler) -> Any:
        """Subscribe to lifecycle events (supports ``*`` wildcards)."""
        return self.events.on(event, handler)

    def register_pipeline(self, name: str, pipeline: Pipeline) -> Runtime:
        self._pipelines[name] = pipeline
        return self

    def get_pipeline(self, name: str) -> Pipeline:
        try:
            return self._pipelines[name]
        except KeyError:
            raise PipelineNotFoundError(
                f"pipeline {name!r} is not registered (have: {sorted(self._pipelines)})"
            ) from None

    # -- execution ------------------------------------------------------------

    def _resolve_pipeline(self, pipeline: str | Pipeline | None) -> Pipeline:
        if pipeline is None:
            return self.pipeline
        if isinstance(pipeline, str):
            return self.get_pipeline(pipeline)
        return pipeline

    def _make_context(self, input: Any, pipeline: Pipeline, **kwargs: Any) -> RequestContext:
        return RequestContext(input=input, pipeline_name=pipeline.name, **kwargs)

    async def execute(
        self,
        input: Any,
        *,
        pipeline: str | Pipeline | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_metadata: dict[str, Any] | None = None,
        filters: dict[str, Any] | None = None,
        **provider_options: Any,
    ) -> ExecutionResult:
        resolved = self._resolve_pipeline(pipeline)
        ctx = self._make_context(
            input,
            resolved,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata or {},
        )
        ctx.model = model
        # None from ContextResolver's own constructor default; "" clears it
        # for this call. Not to be confused with metadata= above, which is
        # app-level (ctx.metadata) and never reaches the provider — see
        # provider_metadata below for that.
        ctx.system_prompt = system_prompt
        if filters:
            ctx.state["filters"] = filters
        # provider_metadata= is forwarded as-is into the provider payload
        # (e.g. Anthropic's top-level `metadata` field, {"user_id": ...} for
        # audit correlation) — distinct from metadata= above.
        if provider_metadata is not None:
            provider_options["metadata"] = provider_metadata
        if provider_options:
            ctx.state["provider_options"] = provider_options

        await self.events.emit(ev.REQUEST_RECEIVED, ctx=ctx)
        try:
            await self.middleware.execute(
                ctx, lambda c: resolved.execute(c, bus=self.events)
            )
        except Exception:
            await self.events.emit(ev.REQUEST_FAILED, ctx=ctx)
            raise

        if ctx.response is None:
            raise ConfigurationError(
                "pipeline finished without producing a response — no provider stage ran "
                "(configure llm=/providers= or add a terminal stage)"
            )

        await self._write_back_cache(ctx)
        await self.events.emit(ev.REQUEST_COMPLETED, ctx=ctx)
        return ExecutionResult(
            content=ctx.response,
            context=ctx,
            usage=ctx.usage,
            cached=ctx.cached,
            model=ctx.model,
            provider=ctx.provider,
            metadata=ctx.metadata,
            finish_reason=ctx.finish_reason,
            raw=ctx.raw_response,
        )

    async def stream(
        self,
        input: Any,
        *,
        pipeline: str | Pipeline | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_metadata: dict[str, Any] | None = None,
        filters: dict[str, Any] | None = None,
        **provider_options: Any,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Run the pipeline in streaming mode and yield token chunks.

        The pipeline prepares the context (history, cache policy, retrieval,
        prompt); the terminal provider call streams. A middleware/stage that
        short-circuits with a response yields a single chunk.
        """
        if self.router is None:
            raise ConfigurationError("streaming requires configured providers (llm=/providers=)")
        resolved = self._resolve_pipeline(pipeline)
        ctx = self._make_context(
            input,
            resolved,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata or {},
        )
        ctx.model = model
        ctx.system_prompt = system_prompt
        ctx.state[STATE_STREAMING] = True
        if filters:
            ctx.state["filters"] = filters
        if provider_metadata is not None:
            provider_options["metadata"] = provider_metadata
        if provider_options:
            ctx.state["provider_options"] = provider_options

        await self.events.emit(ev.REQUEST_RECEIVED, ctx=ctx)
        try:
            await self.middleware.execute(
                ctx, lambda c: resolved.execute(c, bus=self.events)
            )
        except Exception:
            await self.events.emit(ev.REQUEST_FAILED, ctx=ctx)
            raise

        if ctx.short_circuited and ctx.response is not None:
            yield StreamChunk(delta=ctx.response)
            yield StreamChunk(
                done=True, model=ctx.model, provider=ctx.provider,
                cached=ctx.cached, request_id=ctx.request_id,
                finish_reason=ctx.finish_reason,
            )
            await self.events.emit(ev.REQUEST_COMPLETED, ctx=ctx)
            return

        # Stages may have adjusted the options during the pipeline run.
        options = dict(ctx.state.get("provider_options", {}))
        if ctx.model:
            options["model"] = ctx.model
        parts: list[str] = []
        async for chunk in self.router.stream(ctx.messages, **options):
            if chunk.done:
                ctx.model = chunk.model or ctx.model
                ctx.provider = chunk.provider or ctx.provider
                ctx.finish_reason = chunk.finish_reason
                ctx.raw_response = chunk.raw
                if chunk.usage:
                    ctx.usage.add(chunk.usage)
                # A new chunk (not the provider's raw one) so cached/request_id
                # — which the provider adapter has no knowledge of — ride the
                # final frame too, matching ExecutionResult's full result shape.
                # raw carries forward (e.g. the provider's own response id, full
                # tool_use content) so REQUEST_COMPLETED subscribers — audit
                # logging, usage recording — have the same escape hatch
                # execute() gives via ExecutionResult.raw.
                yield StreamChunk(
                    done=True, model=ctx.model, provider=ctx.provider, usage=chunk.usage,
                    cached=ctx.cached, request_id=ctx.request_id,
                    finish_reason=ctx.finish_reason, raw=chunk.raw,
                )
            else:
                parts.append(chunk.delta)
                yield chunk
        ctx.response = "".join(parts)
        # Exact-match cache skips streaming (no STATE_CACHE_KEY set), but the
        # semantic cache stores streamed answers for future intent hits.
        await self._write_back_cache(ctx)
        await self.events.emit(ev.RESPONSE_STREAMED, ctx=ctx)
        await self.events.emit(ev.REQUEST_COMPLETED, ctx=ctx)

    async def _write_back_cache(self, ctx: RequestContext) -> None:
        # A tool-only turn (all content is tool_call chunks, whose delta is
        # always "") leaves ctx.response == "" rather than None — must be
        # excluded the same as None, or a semantically-similar later query
        # gets served this blank string as a cache hit instead of the real
        # answer/tool call.
        if ctx.cached or not ctx.response:
            return
        if self.cache is not None:
            key = ctx.state.get(STATE_CACHE_KEY)
            if key:
                entry = {
                    "content": ctx.response,
                    "model": ctx.model,
                    "provider": ctx.provider,
                    # finish_reason is a plain string, safe to persist. raw is
                    # deliberately excluded — not JSON-safe for every adapter
                    # (an SDK object for Bedrock/Vertex) and cache backends
                    # may serialize this entry (e.g. RedisCache).
                    "finish_reason": ctx.finish_reason,
                }
                try:
                    # No ttl= override here: the cache's own default_ttl (set
                    # via cache={"default_ttl": ...} or on a pre-built
                    # CacheStore instance) governs write-back lifetime — a
                    # second, Runtime-level TTL knob previously shadowed it
                    # unconditionally, silently overriding whatever the cache
                    # itself was configured with.
                    await self.cache.set(key, entry)
                except CacheError as exc:
                    # A cache outage must never fail the request, but operators
                    # need to see it happening.
                    logger.warning(
                        "cache write failed for request %s: %s", ctx.request_id, exc
                    )
        if self.semantic_cache is not None:
            embedding = ctx.state.get(STATE_SEMANTIC_EMBEDDING)
            if embedding is not None:
                try:
                    await self.semantic_cache.add(embedding, ctx.response)
                except ByoAIError as exc:
                    # e.g. a zero-magnitude embedding — a cache write failure
                    # must never fail an already-answered request.
                    logger.warning(
                        "semantic cache write failed for request %s: %s",
                        ctx.request_id,
                        exc,
                    )

    async def aclose(self) -> None:
        """Alias for :meth:`close`, matching async-native naming (httpx, anyio)."""
        await self.close()

    async def close(self) -> None:
        """Close every adapter the runtime owns (providers, caches, vector
        stores, embedder, telemetry). Prefer ``async with Runtime(...)``."""
        if self.router is not None:
            await self.router.close()
        if self.cache is not None:
            await self.cache.close()
        if self.vector_store is not None:
            await self.vector_store.close()
        if self.semantic_cache is not None:
            await self.semantic_cache.close()
        embedder_close = getattr(self.embedder, "close", None)
        if embedder_close is not None:
            await embedder_close()
        if self._owned_tracer_provider is not None:
            # Flush the exporter's final batch so last-window spans aren't lost.
            await asyncio.to_thread(self._owned_tracer_provider.shutdown)

    async def __aenter__(self) -> Runtime:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

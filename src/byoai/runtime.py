"""The ByoAI Runtime.

Owns execution: middleware, pipelines, events, provider routing, caching and
usage accounting. Owns nothing about *what* the pipeline does — applications
and frameworks compose stages; the runtime executes them.

    runtime = Runtime(llm={"provider": "openai", "model": "gpt-4o"})
    result = await runtime.execute("What are our SLA terms?")
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from . import events as ev
from .cache.base import CacheStore
from .config import (
    build_cache,
    build_embedder,
    build_router,
    build_semantic_cache,
    build_vector_store,
    configure_telemetry,
)
from .context import RequestContext
from .errors import ByoAIError, CacheError, ConfigurationError, PipelineNotFound
from .events import EventBus, EventHandler
from .middleware import MiddlewareChain, MiddlewareLike
from .pipeline import Pipeline
from .providers.base import LLMProvider
from .providers.router import ProviderRouter, RetryPolicy
from .stages import (
    DEFAULT_SEMANTIC_THRESHOLD,
    STATE_CACHE_KEY,
    STATE_SEMANTIC_EMBEDDING,
    STATE_STREAMING,
    CacheLookup,
    ContextResolver,
    ProviderCall,
    SemanticCacheLookup,
)
from .types import ExecutionResult, StreamChunk
from .vector.base import VectorStore


class Runtime:
    def __init__(
        self,
        *,
        llm: dict[str, Any] | None = None,
        providers: list[LLMProvider] | None = None,
        cache: dict[str, Any] | CacheStore | None = None,
        vector_store: dict[str, Any] | VectorStore | None = None,
        semantic_cache: dict[str, Any] | Any | None = None,
        embedder: dict[str, Any] | Any | None = None,
        retry_policy: RetryPolicy | None = None,
        system_prompt: str | None = None,
        cache_ttl: int | None = 3600,
        telemetry: Any | None = None,
    ) -> None:
        self.events = EventBus()
        self.middleware = MiddlewareChain()
        self._pipelines: dict[str, Pipeline] = {}

        self.cache: CacheStore | None = (
            build_cache(cache) if isinstance(cache, dict) else cache
        )
        self.vector_store: VectorStore | None = (
            build_vector_store(vector_store) if isinstance(vector_store, dict) else vector_store
        )

        resolved_providers = list(providers or [])
        if llm is not None:
            resolved_providers = build_router(llm) + resolved_providers
        self.router: ProviderRouter | None = (
            ProviderRouter(resolved_providers, retry_policy=retry_policy, event_bus=self.events)
            if resolved_providers
            else None
        )

        self.embedder = build_embedder(embedder) if isinstance(embedder, dict) else embedder
        self.semantic_cache = (
            build_semantic_cache(semantic_cache)
            if isinstance(semantic_cache, dict)
            else semantic_cache
        )
        self._semantic_threshold = (
            semantic_cache.get("threshold", DEFAULT_SEMANTIC_THRESHOLD)
            if isinstance(semantic_cache, dict)
            else DEFAULT_SEMANTIC_THRESHOLD
        )
        if self.semantic_cache is not None and self.embedder is None:
            raise ConfigurationError(
                "semantic_cache requires an embedder= (config dict or async callable)"
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
        self._cache_ttl = cache_ttl

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
            raise PipelineNotFound(
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
        metadata: dict[str, Any] | None = None,
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
        if filters:
            ctx.state["filters"] = filters
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
        )

    async def stream(
        self,
        input: Any,
        *,
        pipeline: str | Pipeline | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
        filters: dict[str, Any] | None = None,
        **provider_options: Any,
    ) -> AsyncIterator[StreamChunk]:
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
        ctx.state[STATE_STREAMING] = True
        if filters:
            ctx.state["filters"] = filters
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
            yield StreamChunk(done=True)
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
                if chunk.usage:
                    ctx.usage.add(chunk.usage)
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
        if ctx.cached or ctx.response is None:
            return
        if self.cache is not None:
            key = ctx.state.get(STATE_CACHE_KEY)
            if key:
                entry = {
                    "content": ctx.response,
                    "model": ctx.model,
                    "provider": ctx.provider,
                }
                try:
                    await self.cache.set(key, entry, ttl=self._cache_ttl)
                except CacheError:
                    pass  # cache outage must never fail the request
        if self.semantic_cache is not None:
            embedding = ctx.state.get(STATE_SEMANTIC_EMBEDDING)
            if embedding is not None:
                try:
                    await self.semantic_cache.add(embedding, ctx.response)
                except ByoAIError:
                    # e.g. a zero-magnitude embedding — a cache write failure
                    # must never fail an already-answered request.
                    pass

    async def close(self) -> None:
        if self.router is not None:
            await self.router.close()
        if self.cache is not None:
            await self.cache.close()
        if self.vector_store is not None:
            await self.vector_store.close()
        if self.semantic_cache is not None:
            await self.semantic_cache.close()
        if self.embedder is not None and hasattr(self.embedder, "close"):
            await self.embedder.close()
        if self._owned_tracer_provider is not None:
            # Flush the exporter's final batch so last-window spans aren't lost.
            await asyncio.to_thread(self._owned_tracer_provider.shutdown)

    async def __aenter__(self) -> Runtime:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

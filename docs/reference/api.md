# API Reference

Auto-generated from docstrings. For narrative guides, see [Guides](../guides/fastapi.md).

## Runtime

::: byoai.Runtime

## Pipeline

::: byoai.Pipeline

::: byoai.PipelineStage

::: byoai.FunctionStage

## Middleware

::: byoai.Middleware

## Request context & results

::: byoai.RequestContext

::: byoai.ExecutionResult

::: byoai.Message

::: byoai.Usage

::: byoai.StreamChunk

::: byoai.Document

::: byoai.ProviderResponse

## Errors

::: byoai.ByoAIError

::: byoai.ConfigurationError

::: byoai.ProviderError

::: byoai.AllProvidersFailed

::: byoai.RateLimitError

::: byoai.CacheError

::: byoai.VectorStoreError

::: byoai.FilterError

::: byoai.MiddlewareError

::: byoai.PipelineError

::: byoai.PipelineNotFound

## Cache adapters

::: byoai.cache.base.CacheStore

::: byoai.cache.memory.MemoryCache

::: byoai.cache.redis.RedisCache

::: byoai.cache.redis.make_redis_client

## Semantic (intent) cache

::: byoai.cache.semantic.SemanticCacheStore

::: byoai.cache.semantic.MemorySemanticCache

::: byoai.cache.semantic.RedisSemanticCache

## Vector store adapters

::: byoai.vector.base.VectorStore

::: byoai.vector.base.FunctionVectorStore

::: byoai.vector.pgvector.PgVectorStore

::: byoai.vector.qdrant.QdrantVectorStore

::: byoai.vector.pinecone.PineconeVectorStore

## Provider adapters

::: byoai.providers.base.LLMProvider

::: byoai.providers.base.FunctionProvider

::: byoai.providers.router.ProviderRouter

::: byoai.providers.router.RetryPolicy

::: byoai.providers.openai_compat.OpenAICompatProvider

::: byoai.providers.anthropic.AnthropicProvider

::: byoai.providers.anthropic_cloud.AnthropicBedrockProvider

::: byoai.providers.anthropic_cloud.AnthropicVertexProvider

::: byoai.providers.gemini.GeminiProvider

::: byoai.providers.embeddings.OpenAICompatEmbedder

## Pipeline stages

::: byoai.stages.ContextResolver

::: byoai.stages.CacheLookup

::: byoai.stages.SemanticCacheLookup

::: byoai.stages.VectorRetrieve

::: byoai.stages.ProviderCall

## Telemetry (OpenTelemetry)

::: byoai.telemetry.otel.instrument

::: byoai.telemetry.otel.configure_otlp

::: byoai.telemetry.otel.OpenTelemetryMiddleware

## Background workers

::: byoai.workers.RuntimeWorker

::: byoai.workers.JobQueue

::: byoai.workers.Job

::: byoai.workers.MemoryJobQueue

::: byoai.workers.RedisStreamQueue

## FastAPI integration

::: byoai.integrations.fastapi.attach

::: byoai.integrations.fastapi.get_runtime

::: byoai.integrations.fastapi.stream_response

::: byoai.integrations.fastapi.serve_websocket

## Robyn integration

::: byoai.integrations.robyn.attach

::: byoai.integrations.robyn.create_app

## MCP integration

::: byoai.integrations.mcp.create_server

::: byoai.integrations.mcp.attach

::: byoai.integrations.mcp.create_app

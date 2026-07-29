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

## Vector store adapters

::: byoai.vector.base.VectorStore

::: byoai.vector.pgvector.PgVectorStore

## Provider adapters

::: byoai.providers.base.LLMProvider

::: byoai.providers.router.ProviderRouter

::: byoai.providers.router.RetryPolicy

::: byoai.providers.openai_compat.OpenAICompatProvider

::: byoai.providers.anthropic.AnthropicProvider

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

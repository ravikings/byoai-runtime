# Provider routing & fallback

`byoai.providers.router.ProviderRouter` tries the primary provider up to `max_retries` times
(exponential backoff with jitter, honoring a server's `Retry-After`), then moves to the next
provider in the chain. Non-retryable errors (4xx other than 429) skip straight to the next
provider. If every provider fails, `AllProvidersFailed` carries the full list of underlying
errors.

## Configuring a fallback chain

`llm=` accepts a nested `fallback` dict, flattened into an ordered provider list:

```python
runtime = Runtime(
    llm={
        "provider": "openai",
        "model": "gpt-4o",
        "fallback": {
            "provider": "azure_openai",
            "endpoint": "https://prod.openai.azure.com",
            "deployment": "gpt-4-prod",
            "fallback": {
                "provider": "ollama",
                "model": "llama3.1",
            },
        },
    },
)
```

## Built-in providers

All are `httpx`-based. OpenAI-compatible providers share one adapter
(`byoai.providers.openai_compat.OpenAICompatProvider`); `provider` just changes defaults:

| `provider` | Notes |
| --- | --- |
| `openai` | Default `base_url` is the OpenAI API; reads `OPENAI_API_KEY` if `api_key` isn't set. |
| `anthropic` | Separate adapter (`byoai.providers.anthropic.AnthropicProvider`) for Anthropic's native API shape. |
| `gemini` | Separate adapter (`byoai.providers.gemini.GeminiProvider`) for Google's `generateContent` API; reads `GEMINI_API_KEY` or `GOOGLE_API_KEY` if `api_key` isn't set. |
| `azure_openai` | Requires `endpoint` and `deployment` (or falls back to `AZURE_OPENAI_ENDPOINT`); builds the deployment-scoped URL and sends the key via the `api-key` header. |
| `ollama` | Defaults `base_url` to `http://localhost:11434/v1`. |
| `openrouter` | Defaults `base_url` to OpenRouter's API; reads `OPENROUTER_API_KEY` if `api_key` isn't set. |
| `openai_compatible` / `vllm` / `litellm` | Any OpenAI-compatible REST endpoint — requires `base_url`. |

An unrecognized `provider` is resolved through Python entry points under the `byoai.providers`
group before raising `ConfigurationError` — see [Vector stores: custom adapters via
plugins](vector-stores.md#custom-adapters-via-plugins) for how the plugin mechanism works.

## Tuning retries

```python
from byoai import Runtime
from byoai.providers.router import RetryPolicy

runtime = Runtime(
    llm={"provider": "openai", "model": "gpt-4o"},
    retry_policy=RetryPolicy(max_retries=3, base_delay=0.5, max_delay=10.0, jitter=0.25),
)
```

## Streaming fallback semantics

`runtime.stream()` falls back to the next provider only if a provider fails **before** yielding
any content. Once tokens have reached the caller, a mid-stream failure is raised as-is — the
transport has already sent partial output downstream, so silently retrying would duplicate it.

## Constructing providers directly

Pass `providers=[...]` (a list of `LLMProvider` instances) instead of `llm=` for full control,
or combine both — `llm=` providers are tried first. See the [API reference](../reference/api.md)
for adapter constructor signatures.

## Embeddings

`embedder=` builds a `byoai.providers.embeddings.OpenAICompatEmbedder` — any OpenAI-compatible
`/embeddings` endpoint (OpenAI, Azure, Ollama, vLLM, ...). It powers
[vector retrieval](vector-stores.md#rag-retrieval-in-the-pipeline) and the
[semantic cache](semantic-cache.md); apps may also pass any `async (str) -> list[float]`
callable of their own instead of a config dict.

```python
runtime = Runtime(
    llm={"provider": "openai", "model": "gpt-4o"},
    embedder={"provider": "openai", "model": "text-embedding-3-small"},
)
vector = await runtime.embedder("What are our SLA terms?")
```

`max_batch_size` chunks large `embed_batch()` calls into concurrent requests transparently — set
it to the endpoint's per-call input cap (e.g. 2048 for OpenAI) for bulk-ingestion jobs.

An unrecognized embedder `provider` is resolved through the `byoai.embedders` plugin group, same
as vector stores and LLM providers.

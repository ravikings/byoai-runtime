# Provider routing & fallback

`byoai.providers.router.ProviderRouter` tries the primary provider up to `max_retries` times
(exponential backoff with jitter, honoring a server's `Retry-After`), then moves to the next
provider in the chain. Non-retryable errors (4xx other than 429) skip straight to the next
provider. If every provider fails, `AllProvidersFailedError` (the old
`AllProvidersFailed` name still works as an alias) carries the full list of underlying
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
| `bedrock` | Anthropic on AWS Bedrock — the one adapter that isn't `httpx`-only; see [below](#anthropic-on-aws-bedrock-and-google-vertex). |
| `vertex` | Anthropic on Google Vertex AI — same exception; see [below](#anthropic-on-aws-bedrock-and-google-vertex). |

An unrecognized `provider` is resolved through Python entry points under the `byoai.providers`
group before raising `ConfigurationError` — see [Vector stores: custom adapters via
plugins](vector-stores.md#custom-adapters-via-plugins) for how the plugin mechanism works.

Every adapter accepts `retryable_status=` to override which HTTP status codes the
[retry policy](#tuning-retries) treats as transient (default
`{408, 409, 500, 502, 503, 504}`, plus `529` for Anthropic/Bedrock/Vertex), and a path override
(`chat_path=`, `messages_path=`, or `embeddings_path=` depending on the adapter) for gateways
that mount the API at a non-standard route.

### Anthropic on AWS Bedrock and Google Vertex

Unlike every other adapter, `bedrock` and `vertex` depend on the `anthropic` SDK rather than
being hand-rolled `httpx` — Bedrock auth is AWS SigV4 request signing, Vertex auth is GCP OAuth
service-account tokens, and neither is reasonable to hand-roll. Requires the `bedrock` or
`vertex` extra: `pip install "byoai-runtime[bedrock]"` / `"byoai-runtime[vertex]"`.

```python
# model is whatever Bedrock model ID your account has access to in that region —
# check the AWS Bedrock console/docs for the current ID, these change over time.
runtime = Runtime(llm={"provider": "bedrock", "model": "<bedrock-model-id>",
                        "aws_region": "us-east-1"})
```

```python
# model is whatever Vertex model ID/version your project has access to —
# check the Vertex AI Model Garden for the current ID.
runtime = Runtime(llm={"provider": "vertex", "model": "<vertex-model-id>",
                        "project_id": "my-gcp-project", "region": "us-east5"})
```

Only `aws_region` (Bedrock) or `project_id`+`region` (Vertex) are required — each also falls
back to the same environment variables the SDK itself conventionally uses
(`AWS_REGION`/`AWS_DEFAULT_REGION`; `ANTHROPIC_VERTEX_PROJECT_ID`/`GOOGLE_CLOUD_PROJECT` and
`ANTHROPIC_VERTEX_REGION`/`CLOUD_ML_REGION`). Credentials themselves come from the standard AWS
chain / Application Default Credentials unless passed explicitly
(`aws_access_key`/`aws_secret_key`/`aws_session_token`/`aws_profile`, or
`access_token`/`credentials`). Error classification (429 → `RateLimitError` with `Retry-After`
honored, 5xx → retryable) reuses the same `raise_for_status()` every other adapter uses, applied
to the SDK's own underlying `httpx.Response` — so retry/fallback behaves identically to every
other provider.

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

## Bring your own function

For a custom or gateway-wrapped backend — an existing SDK client, an internal compliance
gateway, anything that isn't a plain HTTP endpoint — `providers=` also accepts a bare async
function directly. No class, no `name`/`model` attributes, no `close()` to stub out:

```python
async def my_gateway(messages, **options) -> str:
    response = await my_existing_client.create(
        model=options.get("model", "claude-sonnet-4-5"),
        messages=[{"role": m.role, "content": m.content} for m in messages],
    )
    return response.text

runtime = Runtime(providers=[my_gateway])
result = await runtime.execute("hi", tenant="acme-corp")  # extra kwargs flow through **options
```

The function is auto-wrapped in `byoai.providers.base.FunctionProvider` — the same pattern
`Pipeline.add()` uses for bare pipeline-stage functions and `embedder=` already uses for bare
embedding functions. Return a plain `str` for the common case, or a full `ProviderResponse` when
you want usage/model/finish_reason tracked. To support `runtime.stream()` too, construct
`FunctionProvider(fn, stream_fn=my_stream_fn)` explicitly — `stream_fn` yields either plain `str`
deltas (a trailing `done` chunk is synthesized for you) or full `StreamChunk` objects if you need
control over the final chunk's usage.

`vector_store=` has the same bare-callable support (`FunctionVectorStore`) — see
[Vector stores](vector-stores.md#bring-your-own-function).

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

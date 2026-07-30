# Testing apps built on Runtime

Nothing here needs a real API key, a real Redis, or a real Postgres. `Runtime` already accepts
bare callables and in-memory adapters everywhere it accepts a config dict or a full adapter
class — the same "bring your own function" pattern used for real gateways doubles as the test
double, so there's no separate mocking library to learn.

## A fake provider

`providers=` accepts a bare async function — see [Provider routing & fallback](providers.md#bring-your-own-function)
for the full pattern. For tests, that's your canned response:

```python
import pytest
from byoai import Runtime

async def fake_llm(messages, **options):
    return "canned answer"

@pytest.fixture
def runtime():
    return Runtime(providers=[fake_llm])

async def test_my_agent_answers(runtime):
    result = await runtime.execute("hi")
    assert result.content == "canned answer"
```

Return a full `ProviderResponse` instead of a plain `str` when your test asserts on `usage`,
`model`, or `finish_reason`:

```python
from byoai.types import ProviderResponse, Usage

async def fake_llm(messages, **options):
    return ProviderResponse(
        content="canned answer",
        model="fake-model",
        provider="fake",
        usage=Usage(input_tokens=10, output_tokens=5),
        finish_reason="stop",
    )
```

## Asserting on what your app sent

`fake_llm` receives the same `messages`/`**options` a real adapter would — inspect them
directly, no request-capture harness needed:

```python
async def test_system_prompt_is_set():
    seen = {}

    async def fake_llm(messages, **options):
        seen["messages"] = messages
        return "ok"

    runtime = Runtime(providers=[fake_llm], system_prompt="be brief")
    await runtime.execute("hi")
    assert seen["messages"][0].role == "system"
    assert seen["messages"][0].content == "be brief"
```

## Simulating failures, retries, and fallback

Raise inside the fake function to test error handling. An unhandled exception becomes a
non-retryable `ProviderError` automatically (matching how a real adapter wraps a transport
failure); raise `ProviderError(..., retryable=True)` yourself to exercise `RetryPolicy` and
fallback:

```python
from byoai import ProviderError
from byoai.providers.router import RetryPolicy

async def flaky(messages, **options):
    raise ProviderError("simulated outage", provider="flaky", retryable=True)

async def backup(messages, **options):
    return "from backup"

# max_retries=0 skips real backoff delays — tests shouldn't wait on RetryPolicy's jitter.
runtime = Runtime(providers=[flaky, backup], retry_policy=RetryPolicy(max_retries=0))
result = await runtime.execute("hi")
assert result.provider == "backup"  # fallback walked past the failing one
```

## Testing a tool-calling round trip

Construct whatever `raw` shape your app's tool-call handling reads — no real provider response
required, since `raw` is a plain dict your handler branches on directly:

```python
async def fake_llm(messages, **options):
    return ProviderResponse(
        content="",
        model="fake-model",
        provider="fake",
        finish_reason="tool_use",
        raw={"content": [{"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {}}]},
    )
```

See [Anthropic tool use and content blocks](providers.md#anthropic-tool-use-and-content-blocks)
and [Sending an OpenAI-compatible tool result back](providers.md#sending-an-openai-compatible-tool-result-back)
for the two `raw` shapes your app might need to branch on.

## Caching, semantic caching, and vector retrieval — without Redis/Postgres

`cache={"provider": "memory"}` and `semantic_cache={"provider": "memory"}` are the same
in-process adapters this library's own test suite uses — same TTL/eviction semantics as
`RedisCache`, just no network. `embedder=` accepts a bare callable too, so a semantic-cache test
doesn't need a real embedding API either:

```python
async def fake_llm(messages, **options):
    return "canned answer"

async def fake_embedder(text: str) -> list[float]:
    return [1.0, 0.0] if "sla" in text.lower() else [0.0, 1.0]

runtime = Runtime(
    providers=[fake_llm],
    cache={"provider": "memory"},
    semantic_cache={"provider": "memory", "threshold": 0.9},
    embedder=fake_embedder,
)
```

For a vector store, `vector_store=` takes a bare callable the same way (`FunctionVectorStore`) —
see [Vector stores](vector-stores.md#bring-your-own-function).

## Streaming

Pass `stream_fn=` to `FunctionProvider` explicitly — a bare function given straight to
`providers=` only covers the non-streaming path:

```python
from byoai.providers.base import FunctionProvider

async def fake_llm(messages, **options):
    return "canned answer"  # the non-streaming fallback FunctionProvider still needs

async def fake_stream(messages, **options):
    yield "hel"
    yield "lo"

runtime = Runtime(providers=[FunctionProvider(fake_llm, stream_fn=fake_stream)])
chunks = [c async for c in runtime.stream("hi")]
assert "".join(c.delta for c in chunks if not c.done) == "hello"
```

## Queue workers

`MemoryJobQueue` is the in-process stand-in for `RedisStreamQueue` — same `JobQueue` protocol,
no Redis. Pair with `run_until_idle()` (drains the queue then returns, instead of running
forever) for a batch-style test:

```python
from byoai.workers import Job, MemoryJobQueue, RuntimeWorker

async def test_worker_processes_jobs():
    async def fake_llm(messages, **options):
        return "canned answer"

    runtime = Runtime(providers=[fake_llm])
    queue = MemoryJobQueue()
    job_id = await queue.publish(Job(payload={"input": "hi"}))
    worker = RuntimeWorker(runtime, queue)
    await worker.run_until_idle()
    result = await queue.read_result(job_id)
    assert result["content"] == "canned answer"
```

# Getting Started

## Installation

```bash
pip install byoai-runtime
```

Optional integrations are extras — install only what you need:

```bash
pip install "byoai-runtime[fastapi]"   # FastAPI integration
pip install "byoai-runtime[robyn]"     # Robyn integration
pip install "byoai-runtime[redis]"     # Redis cache / queue
pip install "byoai-runtime[pgvector]"  # pgvector vector store
pip install "byoai-runtime[otel]"      # OpenTelemetry export
pip install "byoai-runtime[all]"       # everything above
```

## Minimal example

```python
import asyncio
from byoai import Runtime

async def main():
    runtime = Runtime(llm={"provider": "openai", "model": "gpt-4o"})
    result = await runtime.execute("What are our enterprise SLA terms?")
    print(result.content, result.usage.total_tokens, result.cached)
    await runtime.close()

asyncio.run(main())
```

`Runtime` also supports `async with` — it closes provider/cache/vector-store connections
automatically:

```python
async with Runtime(llm={"provider": "openai", "model": "gpt-4o"}) as runtime:
    result = await runtime.execute("...")
```

## Connecting to existing infrastructure

Every adapter is configured with a plain dict (or you can construct adapter objects yourself for
full control — see each guide). Nothing here requires a schema migration or vector re-index — a
full worked example wiring Redis, pgvector, and an OpenAI → Azure OpenAI fallback together is in
the [project README](https://github.com/ravikings/byoai-runtime#-quickstart).

For each adapter's individual configuration surface, see
[Caching](guides/caching.md), [Vector stores](guides/vector-stores.md), and
[Provider routing](guides/providers.md), and the [API reference](reference/api.md) for exact
signatures.

## Next steps

- Drop the runtime into an existing app: [FastAPI guide](guides/fastapi.md),
  [Robyn guide](guides/robyn.md).
- Run executions off the request path: [Background workers](guides/workers.md).
- Runnable example apps live in [`examples/`](https://github.com/ravikings/byoai-runtime/tree/main/examples)
  in the repository.

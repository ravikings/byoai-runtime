# Robyn integration

Requires the `robyn` extra: `pip install "byoai-runtime[robyn]"`.

`byoai.integrations.robyn` registers the same transport dialect as the FastAPI integration —
`POST /execute` (JSON), `POST /stream` (SSE), and a `/ws` WebSocket — on Robyn's Rust-powered
HTTP server. Execution semantics are identical across transports; only the framework glue
differs.

## Attach to an existing app

```python
from robyn import Robyn
from byoai import Runtime
from byoai.integrations.robyn import attach

app = Robyn(__file__)
runtime = Runtime(llm={"provider": "openai", "model": "gpt-4o"})
attach(app, runtime)  # adds /byoai/execute, /byoai/stream, /byoai/ws
app.start(port=8080)
```

`attach` accepts a `prefix` (default `/byoai`) and registers a shutdown handler that closes the
runtime's provider/cache/vector-store connections. `stream_media_type` and `stream_headers`
customize the SSE response the same way FastAPI's `stream_response()` does.

## Standalone service

`create_app(runtime)` builds a Robyn app that only serves ByoAI's routes, plus `GET /healthz`:

```python
from byoai.integrations.robyn import create_app

app = create_app(runtime)
app.start(port=8080)
```

Pass `healthz_path=None` to skip registering the health route (e.g. if the host app already has
one); any other keyword arguments forward to `attach()`.

See [`examples/robyn_app/main.py`](https://github.com/ravikings/byoai-runtime/blob/main/examples/robyn_app/main.py)
for a runnable example.

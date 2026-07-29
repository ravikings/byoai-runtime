from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import Depends, FastAPI, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from tests.conftest import FakeProvider  # noqa: E402

from byoai import Runtime  # noqa: E402
from byoai.integrations.fastapi import attach, get_runtime, stream_response  # noqa: E402


def make_app() -> FastAPI:
    app = FastAPI()
    attach(app, Runtime(providers=[FakeProvider()]))

    @app.post("/ask")
    async def ask(body: dict, rt: Runtime = Depends(get_runtime)):
        result = await rt.execute(body["query"])
        return {"content": result.content, "cached": result.cached}

    @app.post("/ask/stream")
    async def ask_stream(body: dict, rt: Runtime = Depends(get_runtime)):
        return stream_response(rt, body["query"])

    return app


def test_ask_roundtrip():
    with TestClient(make_app()) as client:
        response = client.post("/ask", json={"query": "hi"})
        assert response.status_code == 200
        assert response.json() == {"content": "hello from fake", "cached": False}


def test_sse_stream():
    with TestClient(make_app()) as client:
        with client.stream("POST", "/ask/stream", json={"query": "hi"}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = []
            for line in response.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :]))
    text = "".join(e.get("delta", "") for e in events)
    assert text.strip() == "hello from fake"
    assert events[-1]["done"] is True
    assert events[-1]["usage"]["input_tokens"] == 10


def test_websocket_streaming_dialect():
    from byoai.integrations.fastapi import serve_websocket

    app = FastAPI()
    attach(app, Runtime(providers=[FakeProvider(reply="ws works")]))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await serve_websocket(get_runtime(websocket), websocket)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as connection:
            connection.send_text(json.dumps({"input": "hi"}))
            frames = []
            while True:
                frame = json.loads(connection.receive_text())
                frames.append(frame)
                if frame.get("done") or frame.get("error"):
                    break
    text = "".join(frame.get("delta", "") for frame in frames)
    assert text.strip() == "ws works"
    assert frames[-1]["done"] is True

    # a second message on the same connection also works (connection reuse)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as connection:
            for _ in range(2):
                connection.send_text(json.dumps({"input": "hi"}))
                while True:
                    frame = json.loads(connection.receive_text())
                    if frame.get("done"):
                        break


def test_missing_attach_raises():
    from byoai.errors import ConfigurationError

    app = FastAPI()

    @app.get("/x")
    async def x(rt: Runtime = Depends(get_runtime)):
        return {}

    with TestClient(app, raise_server_exceptions=True) as client:
        with pytest.raises(ConfigurationError):
            client.get("/x")

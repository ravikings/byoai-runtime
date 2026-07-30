from __future__ import annotations

import json

import pytest

flask = pytest.importorskip("flask")

from flask import Flask, jsonify, request  # noqa: E402
from tests.conftest import FakeProvider  # noqa: E402

from byoai import Runtime  # noqa: E402
from byoai.integrations.flask import attach, execute, get_runtime, stream_response  # noqa: E402


@pytest.fixture
def flask_apps():
    """Tracks every Flask app attach()ed during a test so its bridge (and the
    background event-loop thread it owns) gets closed on teardown — otherwise
    each test leaks a live thread for the rest of the pytest process."""
    apps: list[Flask] = []
    yield apps
    for app in apps:
        bridge = app.extensions.get("byoai")
        if bridge is not None:
            bridge.close()


def make_app(track: list[Flask], *, providers=None) -> Flask:
    app = Flask(__name__)
    attach(app, Runtime(providers=providers or [FakeProvider()]))
    track.append(app)

    @app.post("/ask")
    def ask():
        result = execute(request.get_json()["query"])
        return jsonify({"content": result.content, "cached": result.cached})

    @app.post("/ask/stream")
    def ask_stream():
        return stream_response(request.get_json()["query"])

    return app


def test_ask_roundtrip(flask_apps):
    app = make_app(flask_apps)
    client = app.test_client()
    response = client.post("/ask", json={"query": "hi"})
    assert response.status_code == 200
    assert response.get_json() == {"content": "hello from fake", "cached": False}


def test_sse_stream(flask_apps):
    app = make_app(flask_apps)
    client = app.test_client()
    response = client.post("/ask/stream", json={"query": "hi"})
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")
    body = response.get_data(as_text=True)
    lines = [line for line in body.split("\n\n") if line.startswith("data:")]
    events = [json.loads(line[len("data:") :]) for line in lines]
    assert events  # at least one frame
    text = "".join(e.get("delta", "") for e in events)
    assert text.strip() == "hello from fake"
    assert events[-1]["done"] is True


def test_sse_stream_carries_tool_call_chunks(flask_apps):
    # Regression: stream_response()'s SSE filter (`if chunk.done or
    # chunk.delta:`) silently dropped every tool_call chunk, since one has
    # delta="" (falsy) and done=False — a forced tool_choice call streamed
    # over this Flask route got zero tool_call frames.
    from tests.conftest import ToolCallingProvider

    app = make_app(flask_apps, providers=[ToolCallingProvider()])
    client = app.test_client()
    response = client.post("/ask/stream", json={"query": "hi"})
    body = response.get_data(as_text=True)
    lines = [line for line in body.split("\n\n") if line.startswith("data:")]
    events = [json.loads(line[len("data:") :]) for line in lines]
    tool_events = [e for e in events if "tool_call" in e]
    assert tool_events == [
        {"delta": "", "tool_call": {"index": 0, "id": "toolu_1", "name": "answer"}},
        {"delta": "", "tool_call": {"index": 0, "partial_json": '{"a": 1}'}},
    ]


def test_attach_twice_on_same_app_returns_same_runtime_and_no_second_bridge(flask_apps):
    app = Flask(__name__)
    flask_apps.append(app)
    runtime = Runtime(providers=[FakeProvider()])
    returned_first = attach(app, runtime)
    bridge_first = app.extensions["byoai"]

    other_runtime = Runtime(providers=[FakeProvider(reply="should never be used")])
    returned_second = attach(app, other_runtime)
    bridge_second = app.extensions["byoai"]

    assert returned_first is runtime
    assert returned_second is runtime  # not other_runtime — idempotent
    assert bridge_first is bridge_second  # no second bridge/thread created
    assert get_runtime(app) is runtime


def test_get_runtime_without_attach_raises_configuration_error():
    from byoai.errors import ConfigurationError

    app = Flask(__name__)
    with pytest.raises(ConfigurationError):
        get_runtime(app)


class _ClosableFakeProvider(FakeProvider):
    """FakeProvider.close() in conftest.py is a no-op `pass`, so it can't
    prove a Runtime was actually closed — this local subclass adds an
    observable flag."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_attach_twice_closes_the_redundant_second_runtime(flask_apps):
    # Regression: a second attach() call on an already-attached app returned
    # the existing runtime (correct — asserted above) but silently leaked the
    # redundant second Runtime, which already opened a real client/connection
    # in __init__. attach() now closes it instead.
    app = Flask(__name__)
    flask_apps.append(app)
    runtime = Runtime(providers=[FakeProvider()])
    attach(app, runtime)

    redundant_provider = _ClosableFakeProvider(reply="should never be used")
    other_runtime = Runtime(providers=[redundant_provider])
    returned = attach(app, other_runtime)

    assert returned is runtime
    assert redundant_provider.closed is True


def test_run_stream_closes_underlying_async_generator_on_early_exit(flask_apps):
    # Regression: _FlaskBridge.run_stream used to leave the async generator
    # (and the provider stream/connection behind it) unclosed on any exit
    # other than natural exhaustion — e.g. an early client disconnect, where
    # Werkzeug's stream_with_context throws GeneratorExit into the sync
    # generator run_stream() returns. It now wraps the loop in try/finally
    # and always calls agen.aclose().
    from collections.abc import Generator
    from typing import cast

    from byoai.integrations.flask import _FlaskBridge

    closed = {"v": False}

    async def wrapped(inner):
        try:
            async for item in inner:
                yield item
        finally:
            closed["v"] = True

    app = Flask(__name__)
    flask_apps.append(app)
    runtime = attach(app, Runtime(providers=[FakeProvider()]))
    bridge = app.extensions["byoai"]
    assert isinstance(bridge, _FlaskBridge)

    agen = wrapped(runtime.stream("hi"))
    gen = cast(Generator, bridge.run_stream(agen))
    next(gen)  # pull exactly one chunk, leaving the stream mid-flight
    gen.close()  # simulates stream_with_context tearing down early

    assert closed["v"] is True

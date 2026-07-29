from __future__ import annotations

import pytest

robyn = pytest.importorskip("robyn")

from robyn.testing import TestClient  # noqa: E402
from tests.conftest import FakeProvider  # noqa: E402

from byoai import Runtime  # noqa: E402
from byoai.integrations.robyn import create_app  # noqa: E402


def make_client() -> TestClient:
    runtime = Runtime(providers=[FakeProvider(reply="hello from fake")])
    return TestClient(create_app(runtime))


def test_execute_roundtrip():
    with make_client() as client:
        response = client.post("/byoai/execute", json_data={"input": "hi"})
        assert response.status_code == 200
        assert response.json()["content"] == "hello from fake"


def test_stream_route_builds_response():
    # Regression test: StreamingResponse(headers=...) previously received a
    # plain dict, and Robyn's SSE-default-header code calls `.set()` on it,
    # which a dict doesn't have — AttributeError inside the route handler.
    with make_client() as client:
        response = client.post("/byoai/stream", json_data={"input": "hi"})
        assert response.status_code == 200


def test_healthz():
    with make_client() as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

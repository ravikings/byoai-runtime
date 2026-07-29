from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from tests.conftest import FakeProvider

from byoai import Runtime

mcp = pytest.importorskip("mcp")

from byoai.integrations.mcp import attach, create_app, create_server  # noqa: E402

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


async def test_tool_registered_and_callable():
    runtime = Runtime(providers=[FakeProvider(reply="mcp works")])
    server = create_server(runtime, name="test-server")
    tools = await server.list_tools()
    assert [t.name for t in tools] == ["execute"]

    result = await server.call_tool("execute", {"input": "hello via mcp"})
    assert result.structured_content["content"] == "mcp works"
    assert result.is_error is False


async def test_tool_error_surfaced_as_structured_result_not_crash():
    runtime = Runtime(providers=[FakeProvider()])
    server = create_server(runtime)
    result = await server.call_tool(
        "execute", {"input": "hi", "pipeline": "no-such-pipeline"}
    )
    assert result.structured_content["error_type"] == "PipelineNotFound"
    # caught and returned as data, not raised as a transport-level error
    assert result.is_error is False


async def test_custom_tool_name_and_description():
    runtime = Runtime(providers=[FakeProvider()])
    server = create_server(runtime, tool_name="ask_byoai", description="Custom desc")
    tools = await server.list_tools()
    assert tools[0].name == "ask_byoai"
    assert tools[0].description == "Custom desc"


async def test_attach_serves_real_requests_through_a_host_fastapi_app():
    # Regression test: attach() previously mounted an endpoint that 500'd on
    # every request (the sub-app's lifespan — which starts the streamable-HTTP
    # session manager — was never entered) at the wrong path (path+path,
    # doubled by mounting an app that already registered path internally).
    # A route-existence check alone doesn't catch either bug; only a real
    # request through the mounted, actually-running app does. Uses a real
    # subprocess server (not TestClient) — MCP's DNS-rebinding protection
    # validates the real Host header, which TestClient's synthetic requests
    # don't satisfy the same way a real client on a real port does.
    pytest.importorskip("fastapi")
    import asyncio
    import socket
    import sys

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "tests._mcp_attach_app:app",
        "--port", str(port), "--log-level", "warning",
        cwd=REPO_ROOT,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient() as http:
            for _ in range(50):
                try:
                    await http.get(f"{base_url}/other", timeout=0.2)
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.1)
            else:
                pytest.fail("server did not start")

            response = await http.get(f"{base_url}/other", timeout=2)
        assert response.json() == {"ok": True}

        async with streamable_http_client(f"{base_url}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("execute", {"input": "hi"})
        assert result.structured_content["content"] == "attach works"
    finally:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)


def test_attach_raises_when_host_app_cannot_hook_lifespan():
    from starlette.applications import Starlette

    from byoai.errors import ConfigurationError

    app = Starlette()  # this Starlette version's Router has no add_event_handler
    with pytest.raises(ConfigurationError):
        attach(app, Runtime(providers=[FakeProvider()]), path="/mcp")


def test_create_app_returns_asgi_app():
    app = create_app(Runtime(providers=[FakeProvider()]))
    assert callable(app)  # ASGI apps are callable(scope, receive, send)

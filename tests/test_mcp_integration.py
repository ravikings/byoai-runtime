from __future__ import annotations

import asyncio
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from tests.conftest import FakeProvider

from byoai import Runtime

mcp = pytest.importorskip("mcp")

from byoai.integrations.mcp import attach, create_app, create_server  # noqa: E402

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


@asynccontextmanager
async def _run_uvicorn(module: str):
    """Launch ``module:app`` as a real subprocess server, yield its base URL
    once it's answering, then tear it down. Used wherever a test needs a real
    TCP connection and real Host header — MCP's DNS-rebinding protection and
    ASGI lifespan behavior don't reproduce faithfully through TestClient.

    Binds the listening socket here and hands the subprocess its file
    descriptor (uvicorn's ``--fd``) rather than picking a port number and
    hoping nothing else grabs it first — no bind-then-close gap, no race.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.set_inheritable(True)
    port = sock.getsockname()[1]

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", f"{module}:app",
        "--fd", str(sock.fileno()), "--log-level", "warning",
        cwd=REPO_ROOT,
        pass_fds=(sock.fileno(),),
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as http:
            for _ in range(50):
                try:
                    await http.get(f"{base_url}/other", timeout=0.2)
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.1)
            else:
                pytest.fail("server did not start")
        yield base_url
    finally:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
        sock.close()


async def test_tool_registered_and_callable():
    runtime = Runtime(providers=[FakeProvider(reply="mcp works")])
    server = create_server(runtime, name="test-server")
    tools = await server.list_tools()
    assert {t.name for t in tools} == {"execute", "execute_stream"}

    result = await server.call_tool("execute", {"input": "hello via mcp"})
    assert result.structured_content["content"] == "mcp works"
    assert result.is_error is False


@pytest.mark.parametrize("tool_name", ["execute", "execute_stream"])
async def test_tool_error_surfaced_as_structured_result_not_crash(tool_name):
    runtime = Runtime(providers=[FakeProvider()])
    server = create_server(runtime)
    result = await server.call_tool(
        tool_name, {"input": "hi", "pipeline": "no-such-pipeline"}
    )
    assert result.structured_content["error_type"] == "PipelineNotFoundError"
    # caught and returned as data, not raised as a transport-level error
    assert result.is_error is False


async def test_custom_tool_name_and_description():
    runtime = Runtime(providers=[FakeProvider()])
    server = create_server(runtime, tool_name="ask_byoai", description="Custom desc")
    tools = {t.name: t for t in await server.list_tools()}
    assert tools["ask_byoai"].description == "Custom desc"


async def test_stream_tool_can_be_disabled():
    runtime = Runtime(providers=[FakeProvider()])
    server = create_server(runtime, stream_tool_name=None)
    assert [t.name for t in await server.list_tools()] == ["execute"]


async def test_stream_tool_degrades_gracefully_without_a_live_session():
    # execute_stream reports progress via ctx.report_progress, which requires
    # a live client session. Calling it locally (no session, as in this test)
    # must not fail the tool call — progress reporting is best-effort UX, the
    # actual streamed result must still come back correctly.
    runtime = Runtime(providers=[FakeProvider(reply="a b c")])
    server = create_server(runtime)
    result = await server.call_tool("execute_stream", {"input": "hi"})
    assert result.is_error is False
    content = result.structured_content
    assert content["content"].strip() == "a b c"
    # same key set as the non-streaming `execute` tool
    assert content.keys() == {"content", "cached", "model", "provider", "usage", "request_id"}


async def test_attach_and_streaming_over_real_http():
    # One shared server for both concerns below (real TCP, real Host header —
    # MCP's DNS-rebinding protection and ASGI lifespan behavior don't
    # reproduce faithfully through TestClient):
    #
    # 1. attach() regression: it previously mounted an endpoint that 500'd on
    #    every request (the sub-app's own ASGI lifespan — which starts the
    #    streamable-HTTP session manager — was never entered) at the wrong
    #    path (path+path, doubled by mounting an app that already registered
    #    path internally). A route-existence check alone doesn't catch
    #    either bug; only a real request through the mounted, running app does.
    # 2. execute_stream actually streams over the wire (not just that it
    #    returns the right final answer): the client must receive one
    #    progress notification per token delta, in order, matching the full
    #    result's content, before the final result.
    pytest.importorskip("fastapi")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with _run_uvicorn("tests._mcp_attach_app") as base_url:
        async with httpx.AsyncClient() as http:
            assert (await http.get(f"{base_url}/other")).json() == {"ok": True}

        async with streamable_http_client(f"{base_url}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                execute_result = await session.call_tool("execute", {"input": "hi"})

                progress_messages: list[str] = []

                async def on_progress(progress, total, message) -> None:
                    progress_messages.append(message or "")

                stream_result = await session.call_tool(
                    "execute_stream", {"input": "hi"}, progress_callback=on_progress
                )

    assert execute_result.structured_content["content"] == "attach works"
    assert progress_messages == ["Hel", "lo ", "strea", "med ", "attach"]
    assert stream_result.structured_content["content"] == "".join(progress_messages)


async def test_stdio_transport():
    # The other MCP entry point besides streamable HTTP — verified separately
    # since it exercises a completely different transport stack (subprocess
    # stdin/stdout framing, not ASGI).
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=[str(Path(__file__).parent / "_mcp_stdio_app.py")],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"execute", "execute_stream"}

            result = await session.call_tool("execute", {"input": "hi"})
            assert result.structured_content["content"] == "stdio works"


def test_attach_raises_when_host_app_cannot_hook_lifespan():
    from starlette.applications import Starlette

    from byoai.errors import ConfigurationError

    app = Starlette()  # this Starlette version's Router has no add_event_handler
    with pytest.raises(ConfigurationError):
        attach(app, Runtime(providers=[FakeProvider()]), path="/mcp")


def test_create_app_returns_asgi_app():
    app = create_app(Runtime(providers=[FakeProvider()]))
    assert callable(app)  # ASGI apps are callable(scope, receive, send)

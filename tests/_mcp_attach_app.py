"""Standalone ASGI app for the attach() end-to-end regression test
(tests/test_mcp_integration.py). Run as a real server, not via TestClient —
MCP's DNS-rebinding protection validates the real Host header, which
TestClient's synthetic requests don't satisfy the same way a real client on
a real port does. Self-contained (no cross-module imports) so it can be
launched as a bare uvicorn subprocess.
"""

from __future__ import annotations

from fastapi import FastAPI

from byoai import ProviderResponse, Runtime, Usage
from byoai.integrations.mcp import attach


class _FakeProvider:
    name = "fake"
    model = "fake-1"

    async def complete(self, messages, **options):
        return ProviderResponse(
            content="attach works", model=self.model, provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def stream(self, messages, **options):
        return
        yield  # pragma: no cover - never reached; makes this an async generator

    async def close(self) -> None:
        pass


app = FastAPI()


@app.get("/other")
async def other():
    return {"ok": True}


attach(app, Runtime(providers=[_FakeProvider()]), path="/mcp")

"""Standalone stdio MCP server for the stdio-transport regression test
(tests/test_mcp_integration.py). Self-contained so it can be launched as a
bare subprocess without cross-module import path issues.
"""

from __future__ import annotations

import asyncio

from byoai import ProviderResponse, Runtime, StreamChunk, Usage
from byoai.integrations.mcp import create_server


class _FakeProvider:
    name = "fake"
    model = "fake-1"

    async def complete(self, messages, **options):
        return ProviderResponse(
            content="stdio works", model=self.model, provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def stream(self, messages, **options):
        for word in ("Hel", "lo ", "stdio"):
            yield StreamChunk(delta=word, model=self.model, provider=self.name)
        yield StreamChunk(
            done=True, model=self.model, provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def close(self) -> None:
        pass


if __name__ == "__main__":
    server = create_server(Runtime(providers=[_FakeProvider()]))
    asyncio.run(server.run_stdio_async())

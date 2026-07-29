"""Standalone fake provider for benchmark scripts (mirrors tests/conftest.py)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from byoai import Message, ProviderResponse, StreamChunk, Usage


class FakeProvider:
    def __init__(self, *, reply: str = "hello") -> None:
        self.name = "fake"
        self.model = "fake-1"
        self.reply = reply

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        return ProviderResponse(
            content=self.reply, model=self.model, provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def stream(self, messages: list[Message], **options: Any) -> AsyncIterator[StreamChunk]:
        for word in self.reply.split(" "):
            yield StreamChunk(delta=word + " ", model=self.model, provider=self.name)
        yield StreamChunk(done=True, model=self.model, provider=self.name,
                          usage=Usage(input_tokens=10, output_tokens=5))

    async def close(self) -> None:
        pass

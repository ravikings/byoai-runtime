from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from byoai.errors import ProviderError
from byoai.types import Message, ProviderResponse, StreamChunk, ToolCallDelta, Usage


class FakeProvider:
    """Scriptable in-memory provider for tests."""

    def __init__(
        self,
        *,
        name: str = "fake",
        model: str = "fake-1",
        reply: str = "hello from fake",
        fail_times: int = 0,
        fail_retryable: bool = True,
        finish_reason: str | None = None,
        raw: Any = None,
    ) -> None:
        self.name = name
        self.model = model
        self.reply = reply
        self.fail_times = fail_times
        self.fail_retryable = fail_retryable
        self.finish_reason = finish_reason
        self.raw = raw
        self.calls = 0

    def _maybe_fail(self) -> None:
        if self.calls <= self.fail_times:
            raise ProviderError(
                f"{self.name}: scripted failure #{self.calls}",
                provider=self.name,
                retryable=self.fail_retryable,
            )

    async def complete(self, messages: list[Message], **options: Any) -> ProviderResponse:
        self.calls += 1
        self._maybe_fail()
        return ProviderResponse(
            content=self.reply,
            model=self.model,
            provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason=self.finish_reason,
            raw=self.raw,
        )

    async def stream(self, messages: list[Message], **options: Any) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        self._maybe_fail()
        for word in self.reply.split(" "):
            yield StreamChunk(delta=word + " ", model=self.model, provider=self.name)
        yield StreamChunk(
            done=True,
            model=self.model,
            provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason=self.finish_reason,
            raw=self.raw,
        )

    async def close(self) -> None:
        pass


class ToolCallingProvider(FakeProvider):
    """A FakeProvider whose stream() yields only tool_call chunks (id/name on
    the first, a partial_json fragment on the second) followed by done — no
    text deltas at all, matching a forced tool_choice turn."""

    async def stream(self, messages: list[Message], **options: Any) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        self._maybe_fail()
        yield StreamChunk(
            tool_call=ToolCallDelta(index=0, id="toolu_1", name="answer"),
            model=self.model,
            provider=self.name,
        )
        yield StreamChunk(
            tool_call=ToolCallDelta(index=0, partial_json='{"a": 1}'),
            model=self.model,
            provider=self.name,
        )
        yield StreamChunk(done=True, model=self.model, provider=self.name)


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()

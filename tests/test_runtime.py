from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from byoai import ConfigurationError, Message, Pipeline, Runtime
from byoai.cache.memory import MemoryCache
from byoai.stages import CacheLookup, ContextResolver, ProviderCall


def make_runtime(**kwargs) -> Runtime:
    return Runtime(providers=[FakeProvider()], **kwargs)


async def test_execute_simple_string_input():
    runtime = make_runtime()
    result = await runtime.execute("hi there")
    assert result.content == "hello from fake"
    assert result.provider == "fake"
    assert result.model == "fake-1"
    assert result.usage.total_tokens == 15
    assert result.cached is False


async def test_execute_messages_input():
    runtime = make_runtime()
    result = await runtime.execute(
        {"messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]}
    )
    assert result.content == "hello from fake"
    assert result.context.messages[0].role == "system"


async def test_execute_unnormalizable_input_raises():
    # Typed runtime errors propagate unwrapped through the pipeline.
    runtime = make_runtime()
    with pytest.raises(ConfigurationError):
        await runtime.execute(42)


async def test_all_providers_failed_propagates_unwrapped():
    from byoai import AllProvidersFailed

    runtime = Runtime(providers=[FakeProvider(fail_times=99, fail_retryable=False)])
    with pytest.raises(AllProvidersFailed) as excinfo:
        await runtime.execute("hi")
    assert len(excinfo.value.errors) == 1


async def test_stream_exposes_provider_options_to_stages():
    seen: dict = {}

    async def spy_stage(ctx):
        seen.update(ctx.state.get("provider_options", {}))

    runtime = make_runtime()
    runtime.pipeline.add(spy_stage)
    _ = [chunk async for chunk in runtime.stream("hi", temperature=0.9)]
    assert seen == {"temperature": 0.9}


async def test_cache_hit_on_second_call():
    cache = MemoryCache()
    provider = FakeProvider()
    runtime = Runtime(providers=[provider], cache=cache)
    events: list[str] = []
    runtime.on("cache.*", lambda event, payload: events.append(event))

    first = await runtime.execute("same question")
    second = await runtime.execute("same question")

    assert first.cached is False
    assert second.cached is True
    assert second.content == first.content
    assert second.model == first.model
    assert second.provider == first.provider
    assert provider.calls == 1  # second call never reached the provider
    assert events == ["cache.miss", "cache.hit"]


async def test_lifecycle_events_emitted():
    runtime = make_runtime()
    seen: list[str] = []
    runtime.on("request.*", lambda event, payload: seen.append(event))
    runtime.on("provider.*", lambda event, payload: seen.append(event))
    await runtime.execute("hi")
    assert seen == ["request.received", "provider.started", "provider.completed",
                    "request.completed"]


async def test_middleware_short_circuit():
    runtime = make_runtime()

    async def guard(ctx, call_next):
        if "blocked" in str(ctx.input):
            ctx.short_circuit("request rejected")
            return
        await call_next(ctx)

    runtime.use(guard)
    result = await runtime.execute("blocked words here")
    assert result.content == "request rejected"

    result = await runtime.execute("fine")
    assert result.content == "hello from fake"


async def test_middleware_wraps_after_pipeline():
    runtime = make_runtime()
    order: list[str] = []

    async def outer(ctx, call_next):
        order.append("before")
        await call_next(ctx)
        order.append("after")
        ctx.metadata["elapsed"] = ctx.elapsed_ms

    runtime.use(outer)
    result = await runtime.execute("hi")
    assert order == ["before", "after"]
    assert result.metadata["elapsed"] >= 0


async def test_custom_named_pipeline():
    provider = FakeProvider(reply="custom pipeline reply")
    runtime = Runtime(providers=[provider])

    async def add_disclaimer(ctx):
        ctx.messages.append(Message(role="system", content="always add a disclaimer"))

    from byoai.providers.router import ProviderRouter

    custom = Pipeline("legal")
    custom.add(ContextResolver())
    custom.add(add_disclaimer)
    custom.add(ProviderCall(ProviderRouter([provider])))
    runtime.register_pipeline("legal", custom)

    result = await runtime.execute("question", pipeline="legal")
    assert result.content == "custom pipeline reply"
    assert result.context.pipeline_name == "legal"


async def test_unknown_pipeline_raises():
    runtime = make_runtime()
    from byoai import PipelineNotFound

    with pytest.raises(PipelineNotFound):
        await runtime.execute("hi", pipeline="nope")


async def test_no_provider_configured_raises():
    runtime = Runtime()
    with pytest.raises(ConfigurationError):
        await runtime.execute("hi")


async def test_stream_yields_tokens_and_final_usage():
    runtime = make_runtime()
    chunks = [chunk async for chunk in runtime.stream("hi")]
    text = "".join(c.delta for c in chunks if not c.done)
    assert text.strip() == "hello from fake"
    assert chunks[-1].done is True
    final_usage = chunks[-1].usage
    assert final_usage is not None and final_usage.total_tokens == 15


async def test_stream_short_circuit_yields_single_chunk():
    runtime = make_runtime()

    async def guard(ctx, call_next):
        ctx.short_circuit("nope")

    runtime.use(guard)
    chunks = [chunk async for chunk in runtime.stream("hi")]
    assert [c.delta for c in chunks if not c.done] == ["nope"]


async def test_streaming_not_cached_but_execute_is():
    cache = MemoryCache()
    provider = FakeProvider()
    runtime = Runtime(providers=[provider], cache=cache)
    _ = [chunk async for chunk in runtime.stream("q")]
    assert provider.calls == 1
    _ = [chunk async for chunk in runtime.stream("q")]
    assert provider.calls == 2  # streaming bypasses exact-match cache


async def test_session_history_read_from_cache_reader():
    cache = MemoryCache(
        session_reader={"pattern": "app:users:{user_id}:chat_history"},
        session_data={
            "app:users:usr_1:chat_history": [
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
            ]
        },
    )
    runtime = Runtime(providers=[FakeProvider()], cache=cache)
    result = await runtime.execute("follow-up", user_id="usr_1")
    roles = [m.role for m in result.context.messages]
    contents = [m.content for m in result.context.messages]
    assert roles == ["user", "assistant", "user"]
    assert contents[0] == "earlier question"
    assert contents[-1] == "follow-up"


async def test_default_pipeline_stage_composition():
    runtime = Runtime(providers=[FakeProvider()], cache=MemoryCache())
    stage_types = [type(s) for s in runtime.pipeline.stages]
    assert stage_types == [ContextResolver, CacheLookup, ProviderCall]

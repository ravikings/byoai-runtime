from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from byoai import ConfigurationError, Message, Pipeline, Runtime
from byoai.cache.memory import MemoryCache
from byoai.context import RequestContext
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


async def test_cache_hit_preserves_finish_reason():
    # Regression: a cache hit used to always return finish_reason=None on
    # ExecutionResult even when the original (cached) response had one (e.g.
    # "tool_use") — _write_back_cache now stores finish_reason in the cache
    # entry and CacheLookup.execute's hit-restore path restores it.
    cache = MemoryCache()
    provider = FakeProvider(finish_reason="tool_use")
    runtime = Runtime(providers=[provider], cache=cache)

    first = await runtime.execute("same query")
    second = await runtime.execute("same query")

    assert first.cached is False
    assert first.finish_reason == "tool_use"
    assert second.cached is True
    assert second.finish_reason == "tool_use"


async def test_cache_hit_ignores_differing_provider_metadata():
    # Regression: provider_metadata= (e.g. {"user_id": ...} for audit
    # correlation) landed in ctx.state["provider_options"]["metadata"], which
    # CacheLookup.fingerprint() hashed wholesale — so two otherwise-identical
    # requests differing only in a per-request audit tag never hit the exact-
    # match cache, even though provider_metadata never affects what answer
    # comes back.
    cache = MemoryCache()
    provider = FakeProvider()
    runtime = Runtime(providers=[provider], cache=cache)

    first = await runtime.execute("same query", provider_metadata={"user_id": "u1"})
    second = await runtime.execute("same query", provider_metadata={"user_id": "u2"})

    assert first.cached is False
    assert second.cached is True
    assert provider.calls == 1


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


async def test_stream_propagates_raw_to_final_chunk_and_context():
    # Regression: Runtime.stream()'s final chunk never carried raw= (unlike
    # ExecutionResult.raw for execute()), so a REQUEST_COMPLETED subscriber
    # doing audit logging had no provider response id / full content to read.
    provider = FakeProvider(raw={"id": "resp_1", "stop_reason": "tool_use"})
    runtime = Runtime(providers=[provider])
    seen_ctx = {}
    runtime.on("request.completed", lambda event, payload: seen_ctx.update(ctx=payload["ctx"]))

    chunks = [chunk async for chunk in runtime.stream("hi")]
    assert chunks[-1].raw == {"id": "resp_1", "stop_reason": "tool_use"}
    assert seen_ctx["ctx"].raw_response == {"id": "resp_1", "stop_reason": "tool_use"}


async def test_stream_forwards_tool_call_chunks_untouched():
    from tests.conftest import ToolCallingProvider

    runtime = Runtime(providers=[ToolCallingProvider()])
    chunks = [chunk async for chunk in runtime.stream("hi")]
    tool_calls = [c.tool_call for c in chunks if c.tool_call is not None]
    assert [tc.partial_json for tc in tool_calls] == ["", '{"a": 1}']
    assert tool_calls[0].id == "toolu_1"


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


# -- system_prompt= per-call override ----------------------------------------


async def test_system_prompt_constructor_default_used_when_not_overridden():
    runtime = Runtime(providers=[FakeProvider()], system_prompt="be nice")
    result = await runtime.execute("hi")
    assert result.context.messages[0].role == "system"
    assert result.context.messages[0].content == "be nice"


async def test_system_prompt_per_call_override_replaces_constructor_default():
    runtime = Runtime(providers=[FakeProvider()], system_prompt="be nice")
    result = await runtime.execute("hi", system_prompt="be terse")
    assert result.context.messages[0].role == "system"
    assert result.context.messages[0].content == "be terse"


async def test_system_prompt_empty_string_clears_constructor_default_for_call():
    runtime = Runtime(providers=[FakeProvider()], system_prompt="be nice")
    result = await runtime.execute("hi", system_prompt="")
    # No system message at all — "" clears it for this call.
    assert [m.role for m in result.context.messages] == ["user"]


async def test_system_prompt_not_passed_no_constructor_default_means_no_system_message():
    runtime = make_runtime()  # no system_prompt= at construction
    result = await runtime.execute("hi")
    assert [m.role for m in result.context.messages] == ["user"]


# -- provider_metadata= flows into ctx.state["provider_options"]["metadata"] --


async def test_provider_metadata_flows_into_provider_options_metadata():
    runtime = make_runtime()
    result = await runtime.execute("hi", provider_metadata={"user_id": "u1"})
    assert result.context.state["provider_options"]["metadata"] == {"user_id": "u1"}


async def test_provider_metadata_reaches_the_provider_call():
    captured: dict = {}

    class CapturingProvider(FakeProvider):
        async def complete(self, messages, **options):
            captured.update(options)
            return await super().complete(messages, **options)

    runtime = Runtime(providers=[CapturingProvider()])
    await runtime.execute("hi", provider_metadata={"user_id": "u1"})
    assert captured.get("metadata") == {"user_id": "u1"}


async def test_provider_metadata_omitted_leaves_no_metadata_key():
    runtime = make_runtime()
    result = await runtime.execute("hi")
    assert "metadata" not in result.context.state.get("provider_options", {})


# -- ExecutionResult.finish_reason / .raw ------------------------------------


async def test_execution_result_surfaces_finish_reason_and_raw():
    runtime = Runtime(providers=[FakeProvider(finish_reason="tool_use", raw={"foo": "bar"})])
    result = await runtime.execute("hi")
    assert result.finish_reason == "tool_use"
    assert result.raw == {"foo": "bar"}


async def test_execution_result_finish_reason_and_raw_default_to_none():
    runtime = make_runtime()
    result = await runtime.execute("hi")
    assert result.finish_reason is None
    assert result.raw is None


# -- _coerce_message: content pass-through vs. stringification --------------


def test_coerce_message_list_content_passes_through():
    from byoai.stages import _coerce_message

    blocks = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
    message = _coerce_message({"role": "user", "content": blocks})
    assert message is not None
    assert message.content is blocks


def test_coerce_message_non_str_non_list_scalar_is_stringified():
    from byoai.stages import _coerce_message

    message = _coerce_message({"role": "user", "content": 42})
    assert message is not None
    assert message.content == "42"


# -- _last_user_message: text-block extraction from list-valued content -----


def test_last_user_message_extracts_text_block_from_mixed_list_content():
    from byoai.stages import _last_user_message

    ctx = RequestContext(input="x")
    ctx.messages = [
        Message(
            role="user",
            content=[
                {"type": "tool_result", "tool_use_id": "t1", "content": "42"},
                {"type": "text", "text": "what's the weather?"},
            ],
        )
    ]
    assert _last_user_message(ctx) == "what's the weather?"


def test_last_user_message_returns_none_for_all_tool_result_content():
    from byoai.stages import _last_user_message

    ctx = RequestContext(input="x")
    ctx.messages = [
        Message(
            role="user",
            content=[{"type": "tool_result", "tool_use_id": "t1", "content": "42"}],
        )
    ]
    assert _last_user_message(ctx) is None


def test_last_user_message_returns_none_when_no_user_message_at_all():
    from byoai.stages import _last_user_message

    ctx = RequestContext(input="x")
    ctx.messages = [Message(role="system", content="be terse")]
    assert _last_user_message(ctx) is None


async def test_semantic_cache_lookup_degrades_to_noop_on_pure_tool_result_turn():
    from byoai.stages import SemanticCacheLookup

    class ExplodingEmbedder:
        async def __call__(self, text: str) -> list[float]:
            raise AssertionError("embedder must not be called for a pure tool_result turn")

    stage = SemanticCacheLookup(store=object(), embedder=ExplodingEmbedder())
    ctx = RequestContext(input="x")
    ctx.messages = [
        Message(
            role="user",
            content=[{"type": "tool_result", "tool_use_id": "t1", "content": "42"}],
        )
    ]
    # Must not raise (embedder is never invoked) and must not short-circuit.
    await stage.execute(ctx)
    assert ctx.short_circuited is False

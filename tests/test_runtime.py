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


async def test_session_history_tool_call_only_turn_content_none_is_dropped():
    # Regression: a stored tool-call-only assistant turn (content: null is
    # the standard OpenAI/Anthropic shape when a message is nothing but a
    # tool call) first came back through history as the literal text
    # "None" (fabricating something the model never said), then as an
    # empty string "" — which Anthropic's API rejects outright for any
    # non-final message, failing the whole follow-up request. Dropping the
    # turn entirely fixed both of those, but on its own reintroduced a
    # third failure: with the assistant turn gone, the stored user turn
    # and the new follow-up user turn became two consecutive "user"
    # messages, which Anthropic also rejects (strict alternation). The
    # fix merges them into one turn instead of sending two.
    cache = MemoryCache(
        session_reader={"pattern": "app:users:{user_id}:chat_history"},
        session_data={
            "app:users:usr_1:chat_history": [
                {"role": "user", "content": "what's the weather?"},
                {"role": "assistant", "content": None},
            ]
        },
    )
    runtime = Runtime(providers=[FakeProvider()], cache=cache)
    result = await runtime.execute("follow-up", user_id="usr_1")
    contents = [m.content for m in result.context.messages]
    roles = [m.role for m in result.context.messages]
    assert "None" not in contents
    assert "" not in contents
    assert roles == ["user"]
    assert contents == ["what's the weather?\n\nfollow-up"]


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


def test_coerce_message_none_content_drops_the_message():
    # Regression: content=None (the standard shape for a tool-call-only
    # assistant turn in stored OpenAI/Anthropic history) first fell through
    # the "not str/list -> str(content)" branch, fabricating the literal
    # text "None" as if the model had actually said that; turning it into
    # "" instead wasn't safe either since Anthropic's API rejects any
    # non-final message with empty content, failing the whole request.
    # Dropping the message (returning None) is the only option that
    # neither fabricates text nor breaks the request.
    from byoai.stages import _coerce_message

    assert _coerce_message({"role": "assistant", "content": None}) is None


def test_coerce_message_none_content_on_non_assistant_role_is_not_dropped():
    # Regression: the drop-on-None fix above initially applied to every
    # role, not just assistant — dropping a "tool" message (a tool result)
    # with content: None would orphan the preceding assistant turn's
    # tool_use block, which providers like Anthropic require a matching
    # tool_result to immediately follow. That's a different, likely worse
    # failure than the merely-wrong literal text "None" this role falls
    # through to instead.
    from byoai.stages import _coerce_message

    message = _coerce_message({"role": "tool", "content": None})
    assert message is not None
    assert message.content == "None"


def test_coerce_message_drops_a_message_object_with_none_content_too():
    # Regression: _coerce_message's `isinstance(item, Message): return
    # item` branch returned any Message instance completely unchanged,
    # bypassing the content=None drop check entirely — so a tool-call-only
    # assistant turn supplied as an already-constructed Message object
    # (e.g. a caller reconstructing history as Message objects directly,
    # instead of role/content dicts) sailed through with content=None
    # intact, reaching the provider as an invalid message.
    from byoai.stages import _coerce_message
    from byoai.types import Message

    assert _coerce_message(Message(role="assistant", content=None)) is None  # type: ignore[arg-type]

    # Non-assistant roles still fall through unchanged, same as the dict
    # shape above — only assistant content=None is a drop signal.
    tool_message = Message(role="tool", content=None)  # type: ignore[arg-type]
    assert _coerce_message(tool_message) is tool_message


def test_coerce_messages_orphan_dropping_recognizes_message_objects_too():
    # The list-level orphan-dropping/merge logic (_coerce_messages) must
    # recognize a dropped tool-call turn supplied as a Message object the
    # same way it does for the dict shape, not just skip re-coercing it.
    from byoai.stages import _coerce_messages
    from byoai.types import Message

    items = [
        Message(role="user", content="what's the weather?"),
        Message(role="assistant", content=None),  # type: ignore[arg-type]
        Message(role="tool", content="72F and sunny"),
        Message(role="user", content="thanks, one more thing"),
    ]
    messages, _ = _coerce_messages(items)

    assert [(m.role, m.content) for m in messages] == [
        ("user", "what's the weather?\n\nthanks, one more thing"),
    ]


def test_coerce_messages_drops_tool_replies_orphaned_by_a_dropped_assistant_turn():
    # Regression: dropping an assistant tool-call-only turn (content: None)
    # via _coerce_message alone left its "tool" reply in the coerced
    # history, since that decision is made per-item with no visibility into
    # neighboring messages. A "tool" message is only valid immediately after
    # the assistant tool_use turn it responds to — every provider rejects a
    # "tool" message with no preceding tool_use — so once that turn is
    # dropped, sending its orphaned reply just fails the request on the next
    # message instead of the one this fix was meant to stop failing on.
    from byoai.stages import _coerce_messages

    history = [
        {"role": "user", "content": "what's the weather?"},
        {"role": "assistant", "content": None},
        {"role": "tool", "content": "72F and sunny"},
        {"role": "assistant", "content": "It's 72F and sunny."},
    ]
    messages, _ = _coerce_messages(history)

    assert [(m.role, m.content) for m in messages] == [
        ("user", "what's the weather?"),
        ("assistant", "It's 72F and sunny."),
    ]


def test_coerce_messages_keeps_tool_replies_not_orphaned_by_a_drop():
    from byoai.stages import _coerce_messages

    history = [
        {"role": "user", "content": "what's the weather?"},
        {"role": "assistant", "content": "checking..."},
        {"role": "tool", "content": "72F and sunny"},
    ]
    messages, _ = _coerce_messages(history)

    assert [(m.role, m.content) for m in messages] == [
        ("user", "what's the weather?"),
        ("assistant", "checking..."),
        ("tool", "72F and sunny"),
    ]


def test_coerce_messages_does_not_drop_a_tool_reply_after_a_keyless_assistant_dict():
    # Regression: _is_dropped_tool_call_turn used item.get("content") is
    # None to detect a dropped tool-call-only turn — which is also true for
    # a malformed/legacy assistant dict with no "content" key at all.
    # _coerce_message already drops that malformed entry on its own (its
    # "content" in item guard excludes it), but _coerce_messages was
    # additionally, incorrectly, treating it as a *dropped tool-call turn*
    # and deleting the well-formed "tool" reply immediately after it too.
    from byoai.stages import _coerce_messages

    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "text": "malformed, no content key"},
        {"role": "tool", "content": "72F and sunny"},
    ]
    messages, _ = _coerce_messages(history)

    assert [(m.role, m.content) for m in messages] == [
        ("user", "q"),
        ("tool", "72F and sunny"),
    ]


def test_drop_until_valid_start_strips_leading_tool_and_assistant_messages():
    from byoai.stages import _drop_until_valid_start

    messages = [
        Message(role="assistant", content="orphaned by truncation"),
        Message(role="tool", content="orphaned reply"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="not leading, kept"),
    ]
    result = _drop_until_valid_start(messages)

    assert [(m.role, m.content) for m in result] == [
        ("user", "hi"),
        ("assistant", "not leading, kept"),
    ]


def test_drop_until_valid_start_returns_empty_list_when_nothing_is_a_user_turn():
    from byoai.stages import _drop_until_valid_start

    messages = [Message(role="assistant", content="a"), Message(role="tool", content="b")]
    assert _drop_until_valid_start(messages) == []


async def test_session_history_truncation_boundary_does_not_orphan_a_tool_reply():
    # Regression: _history() truncates *after* _coerce_messages() has
    # already run — so a tool_use/tool_result pair that was perfectly
    # intact in the untruncated history (nothing for _coerce_messages() to
    # drop) can still get split by the truncation boundary itself: the
    # assistant tool_use turn falls just before the max_history_messages
    # cutoff, its "tool" reply just after. The truncated history then
    # starts with an orphaned "tool" message — the same failure
    # _coerce_messages() already guards against, just caused by slicing.
    cache = MemoryCache(
        session_reader={"pattern": "app:users:{user_id}:chat_history"},
        session_data={
            "app:users:usr_1:chat_history": [
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "checking..."},
                {"role": "tool", "content": "72F and sunny"},
            ]
        },
    )
    resolver = ContextResolver(cache=cache, max_history_messages=1)
    ctx = RequestContext(input="follow-up", user_id="usr_1")
    await resolver.execute(ctx)

    roles = [m.role for m in ctx.messages]
    assert "tool" not in roles, (
        f"truncated history still starts with an orphaned tool reply: {roles}"
    )


async def test_session_history_truncation_boundary_does_not_start_with_assistant():
    # Regression: the truncation-boundary fix above only stripped a
    # leading "tool" message — but a fixed-size window over a plain,
    # otherwise well-formed alternating conversation (no tool calls
    # involved at all) can just as easily start on "assistant" instead of
    # "user", which Anthropic (and any provider requiring the first
    # message to have role="user") rejects just as fatally.
    cache = MemoryCache(
        session_reader={"pattern": "app:users:{user_id}:chat_history"},
        session_data={
            "app:users:usr_1:chat_history": [
                {"role": "user", "content": "Q0"},
                {"role": "assistant", "content": "A0"},
                {"role": "user", "content": "Q1"},
                {"role": "assistant", "content": "A1"},
            ]
        },
    )
    resolver = ContextResolver(cache=cache, max_history_messages=3)
    ctx = RequestContext(input="follow-up", user_id="usr_1")
    await resolver.execute(ctx)

    roles = [m.role for m in ctx.messages]
    assert roles == ["user", "assistant", "user"], (
        f"3-message window over Q0,A0,Q1,A1 should drop the leading A0, keeping "
        f"Q1,A1 plus the new query: {roles}"
    )


async def test_session_history_trailing_drop_detection_survives_malformed_trailing_item():
    # Regression: a separate helper independently re-scanned the raw
    # history's tail to decide whether _history()'s last coerced message
    # was a "drop survivor" eligible to merge with the new call's input —
    # but it only knew to skip "tool"-role dict items, while
    # _coerce_messages()'s own (correct) logic skips *any* item that
    # doesn't coerce to a message (e.g. a malformed dict with no role/
    # content shape it recognizes), without treating that as ending the
    # pending-merge state either. The two disagreed for a history whose
    # tail was [dropped assistant turn, its tool reply, one more
    # malformed junk item]: _coerce_messages() correctly still considered
    # the last real message a drop survivor, but the separate re-scan
    # said no — so the merge that should have happened at the history/
    # input seam didn't, leaving two consecutive "user" messages, which
    # Anthropic (and any provider requiring strict alternation) rejects
    # outright. Fixed by computing this flag as a direct byproduct of
    # _coerce_messages()'s own single pass instead of a second, separately
    # maintained scan.
    cache = MemoryCache(
        session_reader={"pattern": "app:users:{user_id}:chat_history"},
        session_data={
            "app:users:usr_1:chat_history": [
                {"role": "user", "content": "what's the weather?"},
                {"role": "assistant", "content": None},
                {"role": "tool", "content": "72F and sunny"},
                {"not_a_role_key": True},
            ]
        },
    )
    resolver = ContextResolver(cache=cache)
    ctx = RequestContext(input="follow-up question", user_id="usr_1")
    await resolver.execute(ctx)

    roles = [m.role for m in ctx.messages]
    assert roles == ["user"], (
        f"expected the history-derived turn and the new query to merge into one "
        f"user message, not sit as two consecutive user messages: {roles}"
    )


# -- _coerce_messages / _append_messages: merge only at an actual drop's seam,
#    never just because two adjacent messages happen to share a role --------


def test_coerce_messages_merges_the_seam_around_a_dropped_turn():
    from byoai.stages import _coerce_messages

    items = [
        {"role": "user", "content": "what's the weather?"},
        {"role": "assistant", "content": None},
        {"role": "tool", "content": "72F and sunny"},
        {"role": "user", "content": "thanks, one more thing"},
    ]
    messages, _ = _coerce_messages(items)

    assert [(m.role, m.content) for m in messages] == [
        ("user", "what's the weather?\n\nthanks, one more thing"),
    ]


def test_coerce_messages_does_not_merge_same_role_messages_with_no_drop_between_them():
    # Regression: an earlier version merged *every* adjacent same-role
    # user/assistant pair across the whole final message list, regardless
    # of why they were adjacent — which silently collapsed a caller's own
    # intentionally-distinct same-role turns (e.g. ctx.input supplied
    # directly as a list of Messages, a documented, supported shape) into
    # one, dropping per-turn attribution. Merging is now scoped to
    # exactly the seam left behind by an actual dropped turn (see
    # _append_messages()) — two same-role messages with nothing dropped
    # between them are left alone.
    from byoai.stages import _coerce_messages
    from byoai.types import Message

    items = [
        Message(role="user", content="first", name="alice"),
        Message(role="user", content="second", name="bob"),
    ]
    messages, _ = _coerce_messages(items)

    assert [(m.role, m.content, m.name) for m in messages] == [
        ("user", "first", "alice"),
        ("user", "second", "bob"),
    ]


def test_coerce_messages_does_not_merge_across_a_role_change():
    from byoai.stages import _coerce_messages

    items = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": "actually here's my answer"},
    ]
    messages, _ = _coerce_messages(items)

    assert [(m.role, m.content) for m in messages] == [
        ("user", "q"),
        ("assistant", "actually here's my answer"),
    ]


# -- _merge_messages: how a genuine merge combines content/name/metadata ----


def test_merge_messages_combines_string_content():
    from byoai.stages import _merge_messages
    from byoai.types import Message

    merged = _merge_messages(
        Message(role="user", content="what's the weather?"),
        Message(role="user", content="follow-up"),
    )
    assert merged.content == "what's the weather?\n\nfollow-up"


def test_merge_messages_combines_list_valued_content():
    from byoai.stages import _merge_messages
    from byoai.types import Message

    blocks = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
    merged = _merge_messages(
        Message(role="user", content=blocks),
        Message(role="user", content="a plain-text follow-up"),
    )
    assert merged.content == [*blocks, {"type": "text", "text": "a plain-text follow-up"}]


def test_merge_messages_drops_name_when_it_disagrees():
    # Unlike content, two disagreeing names can't be concatenated into
    # something meaningful — kept only when both sides agree.
    from byoai.stages import _merge_messages
    from byoai.types import Message

    merged = _merge_messages(
        Message(role="user", content="hi", name="alice"),
        Message(role="user", content="bye", name="bob"),
    )
    assert merged.name is None


def test_merge_messages_merges_metadata_dicts():
    from byoai.stages import _merge_messages
    from byoai.types import Message

    merged = _merge_messages(
        Message(role="user", content="hi", metadata={"trace_id": "t1", "shared": "a"}),
        Message(role="user", content="bye", metadata={"request_id": "r2", "shared": "b"}),
    )
    assert merged.metadata == {"trace_id": "t1", "request_id": "r2", "shared": "b"}


def test_merge_messages_tolerates_an_explicit_none_metadata():
    # Regression: Message.metadata's declared type is dict[str, Any] with
    # a default_factory of dict, but nothing at runtime actually stops a
    # caller from constructing one with metadata=None explicitly.
    # _merge_metadata's `if not a and not b:` guard doesn't short-circuit
    # when only one side is None (not both), so it fell through to
    # `{**a, **b}` and raised TypeError: argument of type 'NoneType' is
    # not a mapping.
    from byoai.stages import _merge_messages
    from byoai.types import Message

    merged = _merge_messages(
        Message(role="user", content="hi", metadata=None),  # type: ignore[arg-type]
        Message(role="user", content="bye", metadata={"k": 1}),
    )
    assert merged.metadata == {"k": 1}


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

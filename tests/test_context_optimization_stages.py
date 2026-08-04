"""Behavioral tests for the context-optimization primitives.

These pin the runtime primitives (``byoai.stages.PromptCacheInjection``,
``byoai.stages.SessionDedup``, ``byoai.session_hash.*``) that own request-side
optimization on the proxy's live path. They were originally lifted from legacy
inline proxy functions (``ensure_cache_control`` / ``optimize_payload`` /
``is_duplicate_hash`` / ``add_hash``) and pinned byte-for-byte against them;
those legacy functions have since been deleted (strangler step 4), so the
primitives are now the sole source of truth and are asserted directly here.

Note: these tests import ``byoai.stages``/``byoai.session_hash`` directly and do
NOT import ``tests.conftest``, so they run in this environment even though a
shadowing site-packages ``tests`` package breaks that import for other modules.
"""

from __future__ import annotations

import copy

import pytest

from byoai.context import RequestContext
from byoai.session_hash import InMemoryHashStore, RedisHashStore
from byoai.stages import (
    STATE_ANTHROPIC_BODY,
    STATE_DEDUP_TOKENS,
    PromptCacheInjection,
    SessionDedup,
    _count_cache_control_markers,
)

LONG_TEXT = "x" * 2500
HUGE_LOG = "line\n" * 500  # >1200 chars, log-shaped


class FakeRedis:
    """In-memory stand-in for the subset of redis-py's async API used here."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sets: dict[str, set[str]] = {}

    def _maybe_fail(self) -> None:
        if self.fail:
            raise ConnectionError("redis unavailable (simulated)")

    async def sismember(self, key: str, value: str) -> bool:
        self._maybe_fail()
        return value in self.sets.get(key, set())

    async def sadd(self, key: str, value: str) -> None:
        self._maybe_fail()
        self.sets.setdefault(key, set()).add(value)

    async def expire(self, key: str, seconds: int) -> None:
        self._maybe_fail()


# --------------------------------------------------------------------------
# PromptCacheInjection
# --------------------------------------------------------------------------

CACHE_BODIES = [
    {"system": "you are helpful", "messages": [{"role": "user", "content": "hi"}]},
    {
        "system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        "messages": [{"role": "user", "content": "hi"}],
    },
    {"tools": [{"name": "t1"}, {"name": "t2"}], "messages": []},
    {
        "system": "sys",
        "tools": [{"name": "t1"}],
        "messages": [{"role": "user", "content": "hi"}],
    },
    # Client already set cache_control somewhere -> must be a no-op.
    {
        "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "t1"}],
        "messages": [],
    },
    {"messages": [{"role": "user", "content": "hi"}]},  # nothing to mark
]


@pytest.mark.parametrize("body", CACHE_BODIES)
def test_prompt_cache_injection_is_idempotent(body):
    """Injecting twice is a no-op the second time: the stage never adds more
    breakpoints once cache_control is present anywhere in the body."""
    once = PromptCacheInjection.inject(copy.deepcopy(body))
    twice = PromptCacheInjection.inject(copy.deepcopy(once))
    assert twice == once


def test_prompt_cache_injection_noop_when_client_set_cache_control():
    body = {
        "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "t1"}],
        "messages": [],
    }
    before = copy.deepcopy(body)
    assert PromptCacheInjection.inject(body) == before


def test_prompt_cache_injection_marks_string_system():
    body = {"system": "sys", "messages": [{"role": "user", "content": "hi"}]}
    out = PromptCacheInjection.inject(body)
    assert out["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_prompt_cache_injection_marks_last_tool():
    body = {"tools": [{"name": "t1"}, {"name": "t2"}], "messages": []}
    out = PromptCacheInjection.inject(body)
    assert out["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_prompt_cache_injection_noop_with_nothing_to_mark():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _count_cache_control_markers(PromptCacheInjection.inject(body)) == 0


async def test_prompt_cache_injection_execute_mutates_wire_body():
    body = {"system": "sys", "messages": [{"role": "user", "content": "hi"}]}
    ctx = RequestContext(input=None)
    ctx.state[STATE_ANTHROPIC_BODY] = body
    await PromptCacheInjection().execute(ctx)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_prompt_cache_injection_execute_noop_without_body():
    ctx = RequestContext(input=None)
    await PromptCacheInjection().execute(ctx)  # must not raise


# --------------------------------------------------------------------------
# SessionDedup
# --------------------------------------------------------------------------

def make_body(text: str) -> dict:
    return {
        "model": "claude-x",
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
    }


def make_tool_result_body(tool_name: str, output: str) -> dict:
    return {
        "model": "claude-x",
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tid-1", "name": tool_name}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tid-1", "content": output}
                ],
            },
        ],
    }


def _tool_result_text(body: dict) -> str:
    return body["messages"][1]["content"][0]["content"]


async def test_session_dedup_short_text_untouched():
    store = InMemoryHashStore()
    body, orig, opt = await SessionDedup(store).optimize(make_body("short"), "s")
    assert body["messages"][0]["content"][0]["text"] == "short"


async def test_session_dedup_truncates_noisy_log_tool_output():
    """Log-shaped output from a noisy tool (Bash) is truncated log-like."""
    store = InMemoryHashStore()
    body, *_ = await SessionDedup(store).optimize(
        make_tool_result_body("Bash", HUGE_LOG), "s"
    )
    text = _tool_result_text(body)
    assert "byoai-runtime" in text and "truncated" in text
    assert len(text) < len(HUGE_LOG)


async def test_session_dedup_generic_truncates_non_log_tool_output():
    """Non-log tool output (Read) uses length-based head/tail truncation, never
    keyword reclassification."""
    store = InMemoryHashStore()
    big = "y" * 5000
    body, *_ = await SessionDedup(store).optimize(
        make_tool_result_body("Read", big), "s"
    )
    text = _tool_result_text(body)
    assert "chars omitted from middle (non-log tool output" in text
    assert len(text) < len(big)


async def test_session_dedup_execute_records_token_stats():
    store = InMemoryHashStore()
    ctx = RequestContext(input=None, session_id="s")
    ctx.state[STATE_ANTHROPIC_BODY] = make_body(LONG_TEXT)
    await SessionDedup(store).execute(ctx)
    orig, opt = ctx.state[STATE_DEDUP_TOKENS]
    assert isinstance(orig, int) and isinstance(opt, int)


async def test_session_dedup_collapses_repeat_within_session():
    store = InMemoryHashStore()
    dedup = SessionDedup(store)
    b1, *_ = await dedup.optimize(make_body(LONG_TEXT), "s")
    b2, *_ = await dedup.optimize(make_body(LONG_TEXT), "s")
    assert b1["messages"][0]["content"][0]["text"] == LONG_TEXT
    assert "Duplicate file snapshot detected" in b2["messages"][0]["content"][0]["text"]


async def test_session_dedup_isolates_sessions():
    store = InMemoryHashStore()
    dedup = SessionDedup(store)
    a, *_ = await dedup.optimize(make_body(LONG_TEXT), "a")
    b, *_ = await dedup.optimize(make_body(LONG_TEXT), "b")
    assert a["messages"][0]["content"][0]["text"] == LONG_TEXT
    assert b["messages"][0]["content"][0]["text"] == LONG_TEXT


# --------------------------------------------------------------------------
# Hash stores
# --------------------------------------------------------------------------

async def test_redis_hash_store_happy_path():
    store = RedisHashStore(FakeRedis(), ttl_seconds=60)
    assert await store.is_duplicate("s", "h1") is False
    await store.add("s", "h1")
    assert await store.is_duplicate("s", "h1") is True


async def test_redis_hash_store_falls_back_on_redis_error():
    store = RedisHashStore(FakeRedis(fail=True))
    assert await store.is_duplicate("s", "h") is False
    await store.add("s", "h")  # persists to in-memory fallback despite redis error
    assert await store.is_duplicate("s", "h") is True


async def test_inmemory_hash_store_prune_evicts_oldest():
    store = InMemoryHashStore(max_sessions=2)
    await store.add("s1", "a")
    await store.add("s2", "b")
    await store.add("s3", "c")  # forces prune of oldest (s1)
    assert await store.is_duplicate("s3", "c") is True
    assert len(store._sessions) <= 2

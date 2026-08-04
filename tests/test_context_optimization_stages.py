"""Step 1 regression tests for the extracted context-optimization primitives.

These pin the new runtime primitives (``byoai.stages.PromptCacheInjection``,
``byoai.stages.SessionDedup``, ``byoai.session_hash.*``) to **byte-identical**
output versus the legacy inline proxy functions they were lifted from
(``agent_context_cache/main.py``: ``ensure_cache_control`` / ``optimize_payload``
/ ``is_duplicate_hash`` / ``add_hash``). While both live side by side (until the
proxy is swapped and the legacy code deleted), any divergence is a regression.

Note: these tests import ``byoai.stages``/``byoai.session_hash`` directly and do
NOT import ``tests.conftest``, so they run in this environment even though a
shadowing site-packages ``tests`` package breaks that import for other modules.
"""

from __future__ import annotations

import copy

import pytest

from byoai.agent_context_cache import main as acc_main
from byoai.session_hash import InMemoryHashStore, RedisHashStore
from byoai.stages import (
    STATE_ANTHROPIC_BODY,
    STATE_DEDUP_TOKENS,
    PromptCacheInjection,
    SessionDedup,
)
from byoai.context import RequestContext

LONG_TEXT = "x" * 2500
HUGE_LOG = "line\n" * 500  # >1200 chars, log-shaped


@pytest.fixture(autouse=True)
def _isolate_proxy_globals():
    """These tests reach into the legacy proxy module (``acc_main``) to compare
    against its inline functions, mutating module globals (``r``, the redis
    client, and ``_local_session_hashes``). Save/restore them so nothing leaks
    into the order-sensitive proxy contract tests that share this process."""
    saved_r = acc_main.r
    acc_main._local_session_hashes.clear()
    try:
        yield
    finally:
        acc_main.r = saved_r
        acc_main._local_session_hashes.clear()


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


@pytest.fixture(autouse=True)
def _reset_legacy_module_state():
    """Isolate the legacy proxy's module-level state between tests. Some tests
    here reassign ``acc_main.r`` to a fake for the byte-identical comparison;
    restore the original client and clear the local dedup fallback so nothing
    leaks into the proxy contract tests that share this module global."""
    original_r = acc_main.r
    acc_main._local_session_hashes.clear()
    yield
    acc_main.r = original_r
    acc_main._local_session_hashes.clear()


# --------------------------------------------------------------------------
# PromptCacheInjection == legacy ensure_cache_control
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
def test_prompt_cache_injection_byte_identical_to_legacy(body):
    legacy = acc_main.ensure_cache_control(copy.deepcopy(body))
    new = PromptCacheInjection.inject(copy.deepcopy(body))
    assert new == legacy


def test_marker_count_matches_legacy():
    for body in CACHE_BODIES:
        from byoai.stages import _count_cache_control_markers

        assert _count_cache_control_markers(body) == acc_main._count_cache_control_markers(body)


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
# SessionDedup == legacy optimize_payload
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


DEDUP_BODIES = [
    make_body(LONG_TEXT),
    make_body("short"),
    make_tool_result_body("Bash", HUGE_LOG),          # noisy log tool -> log-like truncation
    make_tool_result_body("Read", "y" * 5000),         # non-log tool -> generic head/tail
    make_tool_result_body("Bash", "no error lines here " * 100),
]


@pytest.mark.parametrize("body", DEDUP_BODIES)
async def test_session_dedup_byte_identical_to_legacy(body):
    # Legacy path uses the module-level fake-less local fallback via monkeypatched r.
    legacy_fake = FakeRedis()
    acc_main.r = legacy_fake
    legacy_body, legacy_orig, legacy_opt = await acc_main.optimize_payload(
        copy.deepcopy(body), "sess-1"
    )

    store = InMemoryHashStore()
    new_body, new_orig, new_opt = await SessionDedup(store).optimize(
        copy.deepcopy(body), "sess-1"
    )

    assert new_body == legacy_body
    assert (new_orig, new_opt) == (legacy_orig, legacy_opt)


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


async def test_session_dedup_execute_records_token_stats():
    store = InMemoryHashStore()
    ctx = RequestContext(input=None, session_id="s")
    ctx.state[STATE_ANTHROPIC_BODY] = make_body(LONG_TEXT)
    await SessionDedup(store).execute(ctx)
    orig, opt = ctx.state[STATE_DEDUP_TOKENS]
    assert isinstance(orig, int) and isinstance(opt, int)


# --------------------------------------------------------------------------
# Hash stores == legacy is_duplicate_hash / add_hash
# --------------------------------------------------------------------------

async def test_redis_hash_store_matches_legacy_happy_path():
    fake = FakeRedis()
    acc_main.r = fake
    store = RedisHashStore(FakeRedis(), ttl_seconds=acc_main.SESSION_TTL_SECONDS)

    legacy_first = await acc_main.is_duplicate_hash("s", "h1")
    new_first = await store.is_duplicate("s", "h1")
    assert legacy_first == new_first is False

    await acc_main.add_hash("s", "h1")
    await store.add("s", "h1")

    assert await acc_main.is_duplicate_hash("s", "h1") is True
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

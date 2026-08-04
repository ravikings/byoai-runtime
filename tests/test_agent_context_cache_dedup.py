"""Regression tests for the text-dedup optimizer's session isolation.

The ``SessionDedup`` stage (``byoai.stages``) replaces a >2000-char text block
with a "duplicate, already seen" placeholder when its hash was already recorded
for that session_id. That's only correct if dedup state truly can't leak between
unrelated sessions — this file's core test
(``test_identical_text_not_collapsed_across_different_sessions``) is the direct
regression test for the corruption scenario described in ``derive_session_id``'s
docstring: two different sessions with byte-identical content must never dedup
against each other.

These drive the stage directly (the legacy inline ``optimize_payload`` it was
lifted from was deleted in strangler step 4). The Redis-down path is exercised
via a failing fake client so the stage falls back to its in-memory store.
"""

from __future__ import annotations

from byoai.session_hash import RedisHashStore
from byoai.stages import SessionDedup

LONG_TEXT = "x" * 2500


def make_body(text: str) -> dict:
    return {
        "model": "claude-x",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
    }


class FakeRedis:
    """In-memory stand-in for the subset of redis-py's async API used here."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.sets: dict[str, set[str]] = {}

    def _maybe_fail(self):
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


def make_dedup(*, fail: bool = False) -> SessionDedup:
    return SessionDedup(RedisHashStore(FakeRedis(fail=fail), ttl_seconds=3600))


async def test_duplicate_text_collapsed_within_same_session():
    dedup = make_dedup()
    session_id = "session-a"

    body1, *_ = await dedup.optimize(make_body(LONG_TEXT), session_id)
    text_block_1 = body1["messages"][0]["content"][0]["text"]
    assert text_block_1 == LONG_TEXT

    body2, *_ = await dedup.optimize(make_body(LONG_TEXT), session_id)
    text_block_2 = body2["messages"][0]["content"][0]["text"]
    assert text_block_2 != LONG_TEXT
    assert "Duplicate file snapshot detected" in text_block_2


async def test_identical_text_not_collapsed_across_different_sessions():
    """
    The core regression test: two unrelated sessions that happen to send the
    exact same large text block must each see it in full — dedup state must
    never leak across session_id boundaries, or one conversation's genuinely
    first-seen content gets replaced with a stale "already seen" placeholder
    for content it never actually received.
    """
    dedup = make_dedup()
    body_a, *_ = await dedup.optimize(make_body(LONG_TEXT), "session-a")
    body_b, *_ = await dedup.optimize(make_body(LONG_TEXT), "session-b")

    assert body_a["messages"][0]["content"][0]["text"] == LONG_TEXT
    assert body_b["messages"][0]["content"][0]["text"] == LONG_TEXT


async def test_dedup_falls_back_to_local_store_when_redis_unavailable():
    dedup = make_dedup(fail=True)
    session_id = "session-fallback"

    body1, *_ = await dedup.optimize(make_body(LONG_TEXT), session_id)
    assert body1["messages"][0]["content"][0]["text"] == LONG_TEXT

    body2, *_ = await dedup.optimize(make_body(LONG_TEXT), session_id)
    assert "Duplicate file snapshot detected" in body2["messages"][0]["content"][0]["text"]


async def test_dedup_fallback_still_isolates_sessions():
    dedup = make_dedup(fail=True)

    body_a, *_ = await dedup.optimize(make_body(LONG_TEXT), "session-a")
    body_b, *_ = await dedup.optimize(make_body(LONG_TEXT), "session-b")

    assert body_a["messages"][0]["content"][0]["text"] == LONG_TEXT
    assert body_b["messages"][0]["content"][0]["text"] == LONG_TEXT

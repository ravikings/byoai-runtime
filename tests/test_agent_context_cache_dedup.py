"""Regression tests for the text-dedup optimizer's session isolation.

optimize_payload replaces a >2000-char text block with a "duplicate, already
seen" placeholder when its hash was already recorded for that session_id
(main.py:optimize_payload). That's only correct if dedup state truly can't
leak between unrelated sessions — this file's core test
(test_identical_text_not_collapsed_across_different_sessions) is the direct
regression test for the corruption scenario described in
derive_session_id's docstring: two different sessions with byte-identical
content must never dedup against each other.
"""

from __future__ import annotations

import pytest

from byoai.agent_context_cache import main as acc_main

LONG_TEXT = "x" * 2500


def make_body(text: str) -> dict:
    return {
        "model": "claude-x",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
    }


class FakeRedis:
    """In-memory stand-in for the subset of redis-py's async API main.py uses."""

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


@pytest.fixture(autouse=True)
def _reset_local_session_fallback():
    """optimize_payload's Redis-down fallback is a module-level dict; tests
    that hit it must not leak state into each other."""
    acc_main._local_session_hashes.clear()
    yield
    acc_main._local_session_hashes.clear()


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(acc_main, "r", fake)
    return fake


async def test_duplicate_text_collapsed_within_same_session(fake_redis):
    session_id = "session-a"

    body1, *_ = await acc_main.optimize_payload(make_body(LONG_TEXT), session_id)
    text_block_1 = body1["messages"][0]["content"][0]["text"]
    assert text_block_1 == LONG_TEXT

    body2, *_ = await acc_main.optimize_payload(make_body(LONG_TEXT), session_id)
    text_block_2 = body2["messages"][0]["content"][0]["text"]
    assert text_block_2 != LONG_TEXT
    assert "Duplicate file snapshot detected" in text_block_2


async def test_identical_text_not_collapsed_across_different_sessions(fake_redis):
    """
    The core regression test: two unrelated sessions that happen to send the
    exact same large text block must each see it in full — dedup state must
    never leak across session_id boundaries, or one conversation's genuinely
    first-seen content gets replaced with a stale "already seen" placeholder
    for content it never actually received.
    """
    body_a, *_ = await acc_main.optimize_payload(make_body(LONG_TEXT), "session-a")
    body_b, *_ = await acc_main.optimize_payload(make_body(LONG_TEXT), "session-b")

    assert body_a["messages"][0]["content"][0]["text"] == LONG_TEXT
    assert body_b["messages"][0]["content"][0]["text"] == LONG_TEXT


async def test_dedup_falls_back_to_local_store_when_redis_unavailable(monkeypatch):
    fake = FakeRedis(fail=True)
    monkeypatch.setattr(acc_main, "r", fake)
    session_id = "session-fallback"

    body1, *_ = await acc_main.optimize_payload(make_body(LONG_TEXT), session_id)
    assert body1["messages"][0]["content"][0]["text"] == LONG_TEXT

    body2, *_ = await acc_main.optimize_payload(make_body(LONG_TEXT), session_id)
    assert "Duplicate file snapshot detected" in body2["messages"][0]["content"][0]["text"]


async def test_dedup_fallback_still_isolates_sessions(monkeypatch):
    fake = FakeRedis(fail=True)
    monkeypatch.setattr(acc_main, "r", fake)

    body_a, *_ = await acc_main.optimize_payload(make_body(LONG_TEXT), "session-a")
    body_b, *_ = await acc_main.optimize_payload(make_body(LONG_TEXT), "session-b")

    assert body_a["messages"][0]["content"][0]["text"] == LONG_TEXT
    assert body_b["messages"][0]["content"][0]["text"] == LONG_TEXT

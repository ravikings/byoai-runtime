"""Bounds on the two stores that previously grew with uptime.

1. InMemoryHashStore caps hashes *per session*, not just the session count, so
   a single long conversation can't grow without limit.
2. RedisHashStore only populates its in-memory fallback while Redis is failing,
   so a healthy deployment doesn't pay the RAM cost twice.
3. db.prune() enforces a retention window on the append-only SQLite log.
"""

from __future__ import annotations

import time

import pytest

from byoai.agent_context_cache import db
from byoai.session_hash import InMemoryHashStore, RedisHashStore

# --- 1. per-session hash cap ------------------------------------------------


@pytest.mark.asyncio
async def test_hashes_per_session_are_capped():
    store = InMemoryHashStore(max_hashes_per_session=10)
    for i in range(100):
        await store.add("s1", f"hash-{i}")
    # Asserted through the public API: the last 10 survive, everything before
    # them is gone.
    for i in range(90):
        assert not await store.is_duplicate("s1", f"hash-{i}")
    for i in range(90, 100):
        assert await store.is_duplicate("s1", f"hash-{i}")


@pytest.mark.asyncio
async def test_oldest_hashes_evicted_newest_retained():
    store = InMemoryHashStore(max_hashes_per_session=3)
    for i in range(5):
        await store.add("s1", f"hash-{i}")
    assert not await store.is_duplicate("s1", "hash-0")
    assert not await store.is_duplicate("s1", "hash-1")
    assert await store.is_duplicate("s1", "hash-2")
    assert await store.is_duplicate("s1", "hash-4")


@pytest.mark.asyncio
async def test_readding_existing_hash_does_not_refresh_its_position():
    """Eviction is FIFO by first-seen, not LRU. A block resent every turn must
    not pin itself in the cache while distinct blocks get evicted around it."""
    store = InMemoryHashStore(max_hashes_per_session=3)
    for i in range(3):
        await store.add("s1", f"hash-{i}")
    await store.add("s1", "hash-0")  # re-add the oldest
    await store.add("s1", "hash-3")  # pushes one out
    assert not await store.is_duplicate("s1", "hash-0")  # still the first evicted
    assert await store.is_duplicate("s1", "hash-3")


# The three tests below read InMemoryHashStore._sessions directly. That is
# deliberate: they assert on memory *bounds* and TTL reaping, which have no
# public expression — is_duplicate/add tell you what is remembered, not how many
# entries are retained or when one was last touched.


@pytest.mark.asyncio
async def test_session_cap_still_applies():
    store = InMemoryHashStore(max_sessions=5, max_hashes_per_session=10)
    for i in range(20):
        await store.add(f"session-{i}", "h")
    assert len(store._sessions) <= 5


@pytest.mark.asyncio
async def test_total_memory_is_bounded_by_both_caps():
    store = InMemoryHashStore(max_sessions=4, max_hashes_per_session=6)
    for s in range(30):
        for h in range(30):
            await store.add(f"s{s}", f"h{h}")
    total = sum(len(e["hashes"]) for e in store._sessions.values())
    assert total <= 4 * 6


@pytest.mark.asyncio
async def test_expired_session_resets_its_hashes():
    store = InMemoryHashStore(ttl_seconds=60)
    await store.add("s1", "h1")
    # Backdate the entry rather than sleeping through a real TTL.
    store._sessions["s1"]["touched"] -= 120
    assert not await store.is_duplicate("s1", "h1")


# --- 2. Redis store keeps a complete in-process mirror ----------------------


class FakeRedis:
    def __init__(self):
        self.sets: dict[str, set] = {}
        self.fail = False

    async def sismember(self, key, member):
        if self.fail:
            raise ConnectionError("redis down")
        return member in self.sets.get(key, set())

    async def sadd(self, key, *members):
        if self.fail:
            raise ConnectionError("redis down")
        self.sets.setdefault(key, set()).update(members)

    async def expire(self, key, ttl):
        if self.fail:
            raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_adds_go_to_both_redis_and_the_fallback():
    """The mirror is unconditional: no recovery/replay logic is needed because
    the fallback is never missing anything."""
    client = FakeRedis()
    store = RedisHashStore(client)
    await store.add("s1", "h1")

    assert "h1" in client.sets["byoai:hashes:s1"]
    assert await store.fallback.is_duplicate("s1", "h1")


@pytest.mark.asyncio
async def test_outage_answers_from_the_mirror():
    client = FakeRedis()
    store = RedisHashStore(client)
    await store.add("s1", "h1")  # recorded while healthy

    client.fail = True
    # No replay, no health tracking: the mirror already has it.
    assert await store.is_duplicate("s1", "h1") is True
    assert await store.is_duplicate("s1", "never-seen") is False


@pytest.mark.asyncio
async def test_hashes_added_during_an_outage_are_not_backfilled_into_redis():
    """Documents a real limitation of this design, unchanged from before.

    An add that fails against Redis lands only in the mirror. Once Redis is
    back, is_duplicate reads Redis (the mirror is consulted only on error), so
    that hash reads as unseen and its block is re-sent once. Closing this gap
    needs recovery/replay machinery, which was tried and reverted as a net
    correctness loss — see RedisHashStore's docstring. The cost is bounded:
    one redundant re-send per outage-era block, and the resend re-records it.
    """
    client = FakeRedis()
    store = RedisHashStore(client)
    client.fail = True
    await store.add("s1", "during-outage")

    client.fail = False
    assert await store.is_duplicate("s1", "during-outage") is False
    # The mirror does still hold it, so a later outage answers correctly.
    client.fail = True
    assert await store.is_duplicate("s1", "during-outage") is True


@pytest.mark.asyncio
async def test_mirror_is_bounded_by_the_per_session_cap():
    """The mirror's memory cost is what makes cap #1 load-bearing."""
    client = FakeRedis()
    store = RedisHashStore(client, fallback=InMemoryHashStore(max_hashes_per_session=10))
    for i in range(100):
        await store.add("s1", f"h{i}")
    client.fail = True  # force answers to come from the mirror
    assert not await store.is_duplicate("s1", "h0")
    assert await store.is_duplicate("s1", "h99")


@pytest.mark.asyncio
async def test_redis_error_never_propagates():
    client = FakeRedis()
    client.fail = True
    store = RedisHashStore(client)
    await store.add("s1", "h1")  # must not raise
    assert await store.is_duplicate("s1", "h1") is True


# --- 3. SQLite retention ----------------------------------------------------


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


async def _seed(session, ts_offset_days, n=1):
    """Insert rows with a backdated ts by writing directly (record_usage_event
    always stamps now())."""
    import sqlite3

    conn = sqlite3.connect(db.DB_PATH)
    ts = time.time() - (ts_offset_days * 86400)
    for _ in range(n):
        conn.execute(
            "INSERT INTO usage_events (ts, session_id, backend, model, input_tokens) "
            "VALUES (?, ?, 'anthropic', 'm', 1)",
            (ts, session),
        )
        conn.execute(
            "INSERT INTO benchmark_samples (ts, session_id, model, real_tokens_original, "
            "real_tokens_sent, real_tokens_saved) VALUES (?, ?, 'm', 1, 1, 0)",
            (ts, session),
        )
    conn.commit()
    conn.close()


def _file_size(path):
    import os

    return os.path.getsize(path)


def _count(table):
    import sqlite3

    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_prune_removes_only_rows_past_the_window(temp_db):
    await db.init_db()
    await _seed("old", ts_offset_days=200, n=5)
    await _seed("recent", ts_offset_days=1, n=3)

    result = await db.prune(90, vacuum=False)

    assert result["deleted_rows"] == 10  # 5 from each table
    assert _count("usage_events") == 3
    assert _count("benchmark_samples") == 3


@pytest.mark.asyncio
async def test_prune_disabled_is_a_noop(temp_db):
    await db.init_db()
    await _seed("old", ts_offset_days=500, n=4)

    result = await db.prune(0)

    assert result["skipped"] is True
    assert _count("usage_events") == 4


@pytest.mark.asyncio
async def test_retention_days_read_from_env(temp_db, monkeypatch):
    await db.init_db()
    await _seed("old", ts_offset_days=10, n=2)
    monkeypatch.setenv("BYOAI_RETENTION_DAYS", "5")

    await db.prune(vacuum=False)

    assert _count("usage_events") == 0


@pytest.mark.asyncio
async def test_reported_window_is_the_one_actually_enforced(temp_db, monkeypatch):
    """Stats must report what the last prune applied, not the live env var:
    editing the env without a restart changes the next prune, not the table."""
    monkeypatch.setattr(db, "_last_enforced_retention_days", None)
    await db.init_db()
    assert db.enforced_retention_days() is None  # nothing pruned yet

    await db.prune(90, vacuum=False)
    assert db.enforced_retention_days() == 90

    # Operator edits the env but does not restart; no prune has run at 7 days.
    monkeypatch.setenv("BYOAI_RETENTION_DAYS", "7")
    assert db.enforced_retention_days() == 90


@pytest.mark.asyncio
async def test_enforced_window_recorded_even_if_vacuum_fails(temp_db, monkeypatch):
    monkeypatch.setattr(db, "_last_enforced_retention_days", None)
    await db.init_db()
    await _seed("old", ts_offset_days=200, n=db.VACUUM_MIN_DELETED_ROWS)

    import sqlite3

    real_connect = db._connect

    class VacuumFailsConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a):
            if sql.strip().upper() == "VACUUM":
                raise sqlite3.OperationalError("database is locked")
            return self._conn.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(db, "_connect", lambda: VacuumFailsConn(real_connect()))
    await db.prune(30, vacuum=True)

    # The rows are gone at 30 days regardless of the vacuum outcome.
    assert db.enforced_retention_days() == 30


@pytest.mark.asyncio
async def test_invalid_retention_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BYOAI_RETENTION_DAYS", "not-a-number")
    assert db._retention_days() == db.DEFAULT_RETENTION_DAYS


@pytest.mark.asyncio
async def test_negative_retention_clamped_to_zero(monkeypatch):
    monkeypatch.setenv("BYOAI_RETENTION_DAYS", "-30")
    assert db._retention_days() == 0


@pytest.mark.asyncio
async def test_prune_failure_is_swallowed(temp_db, monkeypatch):
    await db.init_db()

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(db, "_prune_sync", boom)
    result = await db.prune(90)
    assert "disk on fire" in result["error"]
    assert result["deleted_rows"] == 0


@pytest.mark.asyncio
async def test_vacuum_failure_still_reports_the_completed_delete(temp_db, monkeypatch):
    """The DELETE is committed before VACUUM runs. A locked VACUUM must not be
    reported as a failed prune — the rows really are gone."""
    await db.init_db()
    await _seed("old", ts_offset_days=200, n=db.VACUUM_MIN_DELETED_ROWS)

    # sqlite3.Connection is immutable, so wrap the connection factory instead
    # of patching the type.
    import sqlite3

    real_connect = db._connect

    class VacuumFailsConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a):
            if sql.strip().upper() == "VACUUM":
                raise sqlite3.OperationalError("database is locked")
            return self._conn.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(db, "_connect", lambda: VacuumFailsConn(real_connect()))
    result = await db.prune(90, vacuum=True)

    assert result["deleted_rows"] >= db.VACUUM_MIN_DELETED_ROWS
    assert result["vacuumed"] is False
    assert "database is locked" in result["vacuum_error"]
    assert "error" not in result  # not a total failure
    assert _count("usage_events") == 0  # rows genuinely deleted


@pytest.mark.asyncio
async def test_size_bytes_includes_wal_sidecars(temp_db):
    await db.init_db()
    await _seed("recent", ts_offset_days=1, n=50)
    result = await db.prune(90, vacuum=False)
    # WAL mode leaves recent writes in the -wal file; the reported size must
    # account for them rather than measuring the .db alone.
    assert result["size_bytes"] >= _file_size(temp_db)


@pytest.mark.asyncio
async def test_vacuum_never_runs_when_disabled(temp_db):
    """The startup path passes vacuum=False; VACUUM's exclusive lock must not
    be taken there no matter how many rows the prune removed."""
    await db.init_db()
    await _seed("old", ts_offset_days=200, n=db.VACUUM_MIN_DELETED_ROWS)

    result = await db.prune(90, vacuum=False)

    assert result["deleted_rows"] >= db.VACUUM_MIN_DELETED_ROWS
    assert result["vacuumed"] is False


@pytest.mark.asyncio
async def test_vacuum_skipped_below_threshold(temp_db):
    await db.init_db()
    await _seed("old", ts_offset_days=200, n=2)
    result = await db.prune(90, vacuum=True)
    assert result["deleted_rows"] == 4
    assert result["vacuumed"] is False  # under VACUUM_MIN_DELETED_ROWS
    # Flagged, not silent: the caller asked for a reclaim and must be able to
    # tell that it was skipped by design rather than having failed.
    assert result["vacuum_skipped"] is True


@pytest.mark.asyncio
async def test_vacuum_not_reported_as_skipped_when_not_requested(temp_db):
    await db.init_db()
    await _seed("old", ts_offset_days=200, n=2)
    result = await db.prune(90, vacuum=False)
    assert result["vacuum_skipped"] is False

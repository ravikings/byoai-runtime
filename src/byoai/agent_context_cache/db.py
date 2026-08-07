"""
db.py — durable local record-keeping for byoai-runtime.

Redis (in main.py) holds hot/ephemeral state: session dedup sets with a
TTL, and live running counters for the console/legacy /v1/stats. All of
that is allowed to disappear — a restart, a Redis flush, an eviction under
memory pressure — because none of it needs to survive forever.

This module is the opposite: a permanent, append-only SQLite log, one row
per event, that survives restarts and infrastructure changes. If you want
to point at a number later and say "here's the evidence," it should come
from here, not from a Redis counter that could have been reset an hour
before you looked.

SQLite (not Postgres, not a hosted DB) because this is a single-process
local proxy: no separate database server to run or provision, the entire
history lives in one file you can back up, `sqlite3 byoai_runtime.db` into
directly, or ship elsewhere, and write volume is low — one row per
request at most, benchmark rows only for sampled requests.

All actual sqlite3 calls run inside asyncio.to_thread(), since sqlite3 is
a blocking library and this proxy is otherwise fully async — without that,
every write would briefly stall the event loop for every in-flight
request. A single asyncio.Lock serializes access across threads since we
open a fresh connection per call (check_same_thread=False) rather than
sharing one connection across threads.
"""

import asyncio
import os
import sqlite3
import time

# Default to a stable absolute path under the user's home dir so the durable
# log survives across restarts regardless of which directory the proxy is
# launched from. A relative default would silently create a fresh empty DB in
# each new working directory, stranding earlier data. Override with an absolute
# BYOAI_SQLITE_PATH to relocate it (e.g. to a shared volume).
DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".byoai", "byoai_runtime.db")
DB_PATH = os.getenv("BYOAI_SQLITE_PATH", DEFAULT_DB_PATH)

# How long rows are kept. "Permanent" was the original intent, but an
# append-only log with no ceiling grows unboundedly with uptime (~700 KB/day at
# a few thousand requests) and the unfiltered SUM() queries behind the console
# get slower every week, since they scan the whole table. A long default keeps
# the "point at a number later" use case intact while giving the file a bound.
# Set BYOAI_RETENTION_DAYS=0 to disable pruning and restore the old behavior.
DEFAULT_RETENTION_DAYS = 90


def _retention_days() -> int:
    """Read at call time, not import time, so tests and `byoai-cache prune`
    can override the env var after this module is already imported."""
    raw = os.getenv("BYOAI_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    try:
        days = int(raw)
    except ValueError:
        print(f"[byoai-runtime 🗄️ DB WARNING] invalid BYOAI_RETENTION_DAYS={raw!r}; ignoring")
        return DEFAULT_RETENTION_DAYS
    return max(0, days)


# Reclaiming space means VACUUM, which rewrites the entire database file under
# an EXCLUSIVE lock — unlike the plain DELETE, WAL mode does not let readers
# through it, and a concurrent write from a running proxy can fail with
# "database is locked". So it is opt-in (the startup path passes vacuum=False)
# and, even when requested, only runs if the prune removed enough rows to be
# worth the full-file rewrite.
VACUUM_MIN_DELETED_ROWS = 1_000

# The window actually enforced by the last prune in this process, or None if
# none has run. Reported instead of the live env var: pruning happens once at
# startup, so an operator who edits BYOAI_RETENTION_DAYS without restarting
# would otherwise see stats claim a window that was never applied, while rows
# outside it are still in the table and still counted in the totals.
_last_enforced_retention_days: int | None = None


def enforced_retention_days() -> int | None:
    """The retention window the data actually reflects, or None if no prune
    has run in this process."""
    return _last_enforced_retention_days

_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT NOT NULL,
    model TEXT,
    real_tokens_original INTEGER NOT NULL,
    real_tokens_sent INTEGER NOT NULL,
    real_tokens_saved INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_benchmark_ts ON benchmark_samples(ts);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
"""

_lock = asyncio.Lock()


def _connect():
    # Create the parent dir on demand so the default (~/.byoai/) or any nested
    # BYOAI_SQLITE_PATH works without the user pre-creating it. dirname is "" for
    # a bare relative filename — fall back to "." so makedirs stays valid.
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")  # readers don't block on an in-flight write
    return conn


def _init_sync():
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


async def init_db():
    await asyncio.to_thread(_init_sync)


_PRUNE_TABLES = ("usage_events", "benchmark_samples")


def _db_size_bytes() -> int:
    """On-disk size including the WAL sidecars.

    In WAL mode recent writes live in ``-wal`` until a checkpoint folds them
    into the main file, so measuring the ``.db`` alone understates real disk
    usage — which is the number a caller running `prune` is trying to move.
    """
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = DB_PATH + suffix
        if os.path.exists(path):
            total += os.path.getsize(path)
    return total


def _prune_sync(retention_days, vacuum):
    cutoff = time.time() - (retention_days * 86400)
    conn = _connect()
    try:
        deleted = 0
        for table in _PRUNE_TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            deleted += cur.rowcount or 0
        conn.commit()
        # The DELETE is committed, so this window is now what the data
        # reflects — recorded even if the VACUUM below fails.
        global _last_enforced_retention_days
        _last_enforced_retention_days = retention_days
        vacuumed = False
        vacuum_error = None
        vacuum_skipped = vacuum and deleted < VACUUM_MIN_DELETED_ROWS
        if vacuum and deleted >= VACUUM_MIN_DELETED_ROWS:
            # VACUUM cannot run inside a transaction; sqlite3's implicit one is
            # already closed by the commit above. Its failure is reported
            # separately rather than raised: the DELETE is already committed at
            # this point, so treating a locked VACUUM as a failed prune would
            # tell the caller nothing was deleted when the rows are in fact
            # gone — prompting a pointless retry, or an automation that records
            # the run as a no-op.
            try:
                conn.execute("VACUUM")
                vacuumed = True
            except Exception as e:
                vacuum_error = str(e)
        return {
            "deleted_rows": deleted,
            "vacuumed": vacuumed,
            "vacuum_error": vacuum_error,
            "vacuum_skipped": vacuum_skipped,
            "cutoff_ts": cutoff,
            "retention_days": retention_days,
            "size_bytes": _db_size_bytes(),
        }
    finally:
        conn.close()


async def prune(retention_days: int | None = None, *, vacuum: bool = True) -> dict:
    """Delete rows older than the retention window.

    Returns a summary dict. ``retention_days`` defaults to
    ``BYOAI_RETENTION_DAYS``; 0 disables pruning and is a no-op. Best-effort
    like the write paths — a failure here must never take down the proxy.
    """
    days = _retention_days() if retention_days is None else max(0, retention_days)
    if days == 0:
        return {"deleted_rows": 0, "vacuumed": False, "retention_days": 0, "skipped": True}
    try:
        async with _lock:
            return await asyncio.to_thread(_prune_sync, days, vacuum)
    except Exception as e:
        print(f"[byoai-runtime 🗄️ DB WARNING] retention prune failed: {e}")
        return {"deleted_rows": 0, "vacuumed": False, "retention_days": days, "error": str(e)}


def _insert_benchmark_sync(session_id, model, real_orig, real_sent, real_saved):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO benchmark_samples "
            "(ts, session_id, model, real_tokens_original, real_tokens_sent, real_tokens_saved) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), session_id, model, real_orig, real_sent, real_saved),
        )
        conn.commit()
    finally:
        conn.close()


async def record_benchmark_sample(
    session_id: str, model: str, real_orig: int, real_sent: int, real_saved: int
):
    """Best-effort: a DB write failure must never break the request it
    rides alongside. Caller is expected to fire this without blocking the
    response (see main.py's use of asyncio.create_task)."""
    try:
        async with _lock:
            await asyncio.to_thread(
                _insert_benchmark_sync, session_id, model, real_orig, real_sent, real_saved
            )
    except Exception as e:
        print(f"[byoai-runtime 🗄️ DB WARNING] failed to persist benchmark sample: {e}")


def _insert_usage_sync(session_id, backend, model, usage):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO usage_events "
            "(ts, session_id, backend, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                session_id,
                backend,
                model,
                usage.get("input_tokens", 0) or 0,
                usage.get("output_tokens", 0) or 0,
                usage.get("cache_read_input_tokens", 0) or 0,
                usage.get("cache_creation_input_tokens", 0) or 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def record_usage_event(session_id: str, backend: str, model: str | None, usage: dict | None):
    if not usage:
        return
    try:
        async with _lock:
            await asyncio.to_thread(_insert_usage_sync, session_id, backend, model, usage)
    except Exception as e:
        print(f"[byoai-runtime 🗄️ DB WARNING] failed to persist usage event: {e}")


def _benchmark_summary_sync(since_ts):
    conn = _connect()
    try:
        where = "WHERE ts >= ?" if since_ts else ""
        params = (since_ts,) if since_ts else ()
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(real_tokens_original),0), "
            f"COALESCE(SUM(real_tokens_sent),0), COALESCE(SUM(real_tokens_saved),0) "
            f"FROM benchmark_samples {where}",
            params,
        ).fetchone()
        return {
            "sample_count": row[0],
            "real_tokens_original": row[1],
            "real_tokens_sent": row[2],
            "real_tokens_saved": row[3],
        }
    finally:
        conn.close()


async def benchmark_summary(since_ts: float | None = None) -> dict:
    return await asyncio.to_thread(_benchmark_summary_sync, since_ts)


def _recent_benchmark_samples_sync(limit):
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ts, session_id, model, real_tokens_original, "
            "real_tokens_sent, real_tokens_saved "
            "FROM benchmark_samples ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ts": r[0],
                "session_id": r[1],
                "model": r[2],
                "real_tokens_original": r[3],
                "real_tokens_sent": r[4],
                "real_tokens_saved": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


async def recent_benchmark_samples(limit: int = 50) -> list:
    return await asyncio.to_thread(_recent_benchmark_samples_sync, limit)


def _usage_summary_sync(since_ts):
    conn = _connect()
    try:
        where = "WHERE ts >= ?" if since_ts else ""
        params = (since_ts,) if since_ts else ()
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            f"COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_write_tokens),0) "
            f"FROM usage_events {where}",
            params,
        ).fetchone()
        return {
            "request_count": row[0],
            "total_input_tokens": row[1],
            "total_output_tokens": row[2],
            "total_cache_read_tokens": row[3],
            "total_cache_write_tokens": row[4],
        }
    finally:
        conn.close()


async def usage_summary(since_ts: float | None = None) -> dict:
    return await asyncio.to_thread(_usage_summary_sync, since_ts)

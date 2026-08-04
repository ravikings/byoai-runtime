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

import os
import time
import sqlite3
import asyncio

DB_PATH = os.getenv("BYOAI_SQLITE_PATH", "byoai_runtime.db")

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


async def record_benchmark_sample(session_id: str, model: str, real_orig: int, real_sent: int, real_saved: int):
    """Best-effort: a DB write failure must never break the request it
    rides alongside. Caller is expected to fire this without blocking the
    response (see main.py's use of asyncio.create_task)."""
    try:
        async with _lock:
            await asyncio.to_thread(_insert_benchmark_sync, session_id, model, real_orig, real_sent, real_saved)
    except Exception as e:
        print(f"[byoai-runtime 🗄️ DB WARNING] failed to persist benchmark sample: {e}")


def _insert_usage_sync(session_id, backend, model, usage):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO usage_events "
            "(ts, session_id, backend, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(), session_id, backend, model,
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
            "SELECT ts, session_id, model, real_tokens_original, real_tokens_sent, real_tokens_saved "
            "FROM benchmark_samples ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ts": r[0], "session_id": r[1], "model": r[2],
                "real_tokens_original": r[3], "real_tokens_sent": r[4], "real_tokens_saved": r[5],
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
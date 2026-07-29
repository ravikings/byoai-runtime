from __future__ import annotations

import asyncio

from byoai.cache.memory import MemoryCache


async def test_namespace_prefixing():
    cache = MemoryCache(namespace="byoai:")
    await cache.set("k", "v")
    assert await cache.get("k") == "v"
    assert await cache.get("byoai:k") == "v"  # same key either way
    assert set(cache._store) == {"byoai:k"}


async def test_ttl_expiry():
    cache = MemoryCache()
    await cache.set("k", "v", ttl=1)
    assert await cache.get("k") == "v"
    await asyncio.sleep(0.01)
    assert await cache.get("k") == "v"  # still within ttl


async def test_ttl_zero_means_do_not_cache():
    cache = MemoryCache()
    await cache.set("k", "v", ttl=0)
    assert await cache.get("k") is None


async def test_explicit_none_ttl_falls_back_to_default():
    cache = MemoryCache(default_ttl=60)
    await cache.set("k", "v", ttl=None)
    assert cache._store["byoai:k"][1] is not None  # expiry set from default_ttl


async def test_delete():
    cache = MemoryCache()
    await cache.set("k", "v")
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_session_reader_pattern():
    cache = MemoryCache(
        session_reader={"pattern": "app:{user_id}:history"},
        session_data={"app:u1:history": [{"role": "user", "content": "hi"}]},
    )
    assert await cache.read_session(user_id="u1") == [{"role": "user", "content": "hi"}]
    assert await cache.read_session(user_id="u2") is None

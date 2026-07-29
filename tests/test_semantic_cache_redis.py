from __future__ import annotations

import time

import pytest

from byoai.errors import ConfigurationError

pytest.importorskip("numpy")

from byoai.cache.semantic import RedisSemanticCache  # noqa: E402


class FakeRedisStream:
    """Minimal xadd/xrange stub standing in for one shared Redis server."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, str]]] = []
        self._seq = 0

    async def xadd(self, stream, fields, *, maxlen=None, approximate=True):
        self._seq += 1
        entry_id = f"{self._seq}-0"
        self.entries.append((entry_id, dict(fields)))
        if maxlen is not None and len(self.entries) > maxlen:
            self.entries = self.entries[-maxlen:]
        return entry_id

    async def xrange(self, stream, *, min="-", max="+", count=None):
        if min.startswith("("):
            exclusive_after = min[1:]
            out = [(eid, f) for eid, f in self.entries if _id_gt(eid, exclusive_after)]
        else:
            out = list(self.entries)
        return out[:count] if count else out

    async def aclose(self):
        pass


def _id_gt(a: str, b: str) -> bool:
    a_major, a_minor = (int(x) for x in a.split("-"))
    b_major, b_minor = (int(x) for x in b.split("-"))
    return (a_major, a_minor) > (b_major, b_minor)


def make_pair(server: FakeRedisStream) -> tuple[RedisSemanticCache, RedisSemanticCache]:
    return (
        RedisSemanticCache(client=server, capacity=100),
        RedisSemanticCache(client=server, capacity=100),
    )


async def test_intent_hits_shared_across_workers():
    server = FakeRedisStream()
    worker_a, worker_b = make_pair(server)

    await worker_a.add([1.0, 0.0, 0.0], "answer from A")
    # worker B never saw the add locally; find() syncs from the shared stream
    hit = await worker_b.find([1.0, 0.0, 0.0], threshold=0.99)
    assert hit is not None and hit[0] == "answer from A"


async def test_survives_restart():
    server = FakeRedisStream()
    original = RedisSemanticCache(client=server, capacity=100)
    await original.add([0.0, 1.0], "persisted answer")
    await original.close()

    fresh = RedisSemanticCache(client=server, capacity=100)  # "new process"
    hit = await fresh.find([0.0, 1.0], threshold=0.99)
    assert hit is not None and hit[0] == "persisted answer"


async def test_own_writes_not_duplicated_on_sync():
    server = FakeRedisStream()
    cache = RedisSemanticCache(client=server, capacity=100)
    await cache.add([1.0, 0.0], "mine")
    await cache.find([1.0, 0.0], threshold=0.99)  # sync must not re-add own entry
    assert cache._mirror._count == 1


async def test_expired_entries_not_replayed():
    server = FakeRedisStream()
    writer = RedisSemanticCache(client=server, capacity=100, ttl=3600)
    await writer.add([1.0, 0.0], "stale")
    # rewrite the entry's wall-clock expiry into the past
    entry_id, fields = server.entries[0]
    fields["e"] = str(time.time() - 10)

    reader = RedisSemanticCache(client=server, capacity=100)
    assert await reader.find([1.0, 0.0], threshold=0.99) is None


async def test_embedding_roundtrip_packing():
    packed = RedisSemanticCache._pack([0.25, -1.5, 3.0])
    assert RedisSemanticCache._unpack(packed) == [0.25, -1.5, 3.0]


def test_cluster_mode_config_forwarded(monkeypatch):
    # README-advertised: semantic_cache={"provider": "redis", "mode": "cluster", ...}
    from byoai.config import build_semantic_cache

    captured = {}

    def fake_make_client(**kwargs):
        captured.update(kwargs)
        return FakeRedisStream()

    monkeypatch.setattr("byoai.cache.redis.make_redis_client", fake_make_client)
    build_semantic_cache(
        {"provider": "redis", "mode": "cluster", "url": "redis://node:6379",
         "threshold": 0.9}
    )
    assert captured["mode"] == "cluster"
    assert captured["url"] == "redis://node:6379"


def test_redis_mode_factory_validation():
    from byoai.cache.redis import make_redis_client

    with pytest.raises(ConfigurationError):
        make_redis_client(mode="nope")
    with pytest.raises(ConfigurationError):
        make_redis_client(mode="sentinel")  # missing sentinels/service_name

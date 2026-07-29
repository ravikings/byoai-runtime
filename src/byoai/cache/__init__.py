from .base import CacheStore
from .memory import MemoryCache

__all__ = ["CacheStore", "MemoryCache", "RedisCache"]


def __getattr__(name: str):
    # RedisCache is behind the optional `redis` extra; import lazily.
    if name == "RedisCache":
        from .redis import RedisCache

        return RedisCache
    raise AttributeError(name)

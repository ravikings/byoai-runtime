from typing import TYPE_CHECKING

from .base import CacheStore
from .memory import MemoryCache

if TYPE_CHECKING:
    # Type checkers see this real import (no runtime cost, no redis
    # dependency needed to type-check); __getattr__ below satisfies it at
    # runtime, only importing redis if RedisCache is actually accessed.
    from .redis import RedisCache

__all__ = ["CacheStore", "MemoryCache", "RedisCache"]


def __getattr__(name: str) -> object:
    # RedisCache is behind the optional `redis` extra; import lazily.
    if name == "RedisCache":
        from .redis import RedisCache

        return RedisCache
    raise AttributeError(name)

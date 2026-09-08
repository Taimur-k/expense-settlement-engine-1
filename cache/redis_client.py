"""
Redis Client with In-Memory Fallback
======================================
On startup, attempts to connect to Redis.
If Redis is unavailable, falls back to a thread-safe in-memory dict store.

This allows zero-friction local development without Redis running.
"""
import asyncio
import fnmatch
import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class InMemoryCache:
    """
    Async-interface compatible in-memory key-value store.
    Supports TTL expiry, NX (set-if-not-exists), get/set/delete.
    """

    def __init__(self):
        self._store:  Dict[str, Any]   = {}
        self._expiry: Dict[str, float] = {}  # key -> expiry unix timestamp
        self._lock    = asyncio.Lock()

    def _is_expired(self, key: str) -> bool:
        exp = self._expiry.get(key)
        return exp is not None and time.time() > exp

    def _evict_if_expired(self, key: str) -> bool:
        if self._is_expired(key):
            self._store.pop(key, None)
            self._expiry.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> Optional[str]:
        async with self._lock:
            if self._evict_if_expired(key):
                return None
            return self._store.get(key)

    async def set(
        self, key: str, value: str, ex: Optional[int] = None, nx: bool = False
    ) -> bool:
        async with self._lock:
            self._evict_if_expired(key)
            if nx and key in self._store:
                return False
            self._store[key] = value
            if ex is not None:
                self._expiry[key] = time.time() + ex
            elif key in self._expiry:
                del self._expiry[key]
            return True

    async def delete(self, key: str) -> int:
        async with self._lock:
            existed = key in self._store
            self._store.pop(key, None)
            self._expiry.pop(key, None)
            return 1 if existed else 0

    async def exists(self, key: str) -> int:
        async with self._lock:
            if self._evict_if_expired(key):
                return 0
            return 1 if key in self._store else 0

    async def expire(self, key: str, seconds: int) -> bool:
        async with self._lock:
            if key not in self._store:
                return False
            self._expiry[key] = time.time() + seconds
            return True

    async def ttl(self, key: str) -> int:
        async with self._lock:
            exp = self._expiry.get(key)
            if exp is None:
                return -1
            remaining = int(exp - time.time())
            return max(remaining, 0)

    async def keys(self, pattern: str = "*") -> List[str]:
        async with self._lock:
            now    = time.time()
            live   = [k for k, exp in list(self._expiry.items()) if now <= exp]
            no_exp = [k for k in self._store if k not in self._expiry]
            all_keys = live + no_exp
            if pattern == "*":
                return all_keys
            return [k for k in all_keys if fnmatch.fnmatch(k, pattern)]

    async def ping(self) -> bool:
        return True

    def store_size(self) -> int:
        return len(self._store)


# Global singleton
_cache_instance: Optional[Any] = None


async def init_cache(redis_url: Optional[str] = None) -> Any:
    """
    Initialize the cache.
    Tries Redis first; falls back to InMemoryCache.
    """
    global _cache_instance
    if redis_url:
        try:
            import redis.asyncio as aioredis
            client = aioredis.from_url(redis_url, decode_responses=True)
            await client.ping()
            _cache_instance = client
            log.info("Cache: Connected to Redis at %s", redis_url)
            return client
        except Exception as e:
            log.warning("Cache: Redis unavailable (%s) — using InMemoryCache fallback.", e)

    _cache_instance = InMemoryCache()
    log.info("Cache: Using InMemoryCache fallback.")
    return _cache_instance


def get_cache() -> Any:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = InMemoryCache()
    return _cache_instance


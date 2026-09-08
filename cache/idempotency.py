"""
Idempotency Key Management
===========================
Prevents duplicate expense submissions and double-spending by tracking
unique client-submitted idempotency keys in Redis (or in-memory fallback).

Protocol:
    1. Client sends X-Idempotency-Key: <uuid> header with each POST.
    2. Server checks: SET idempotency:{key} "PROCESSING" NX EX 86400
       - If SET returns True  => first request => proceed
       - If SET returns False => duplicate     => return cached response
    3. On success: update stored value to the JSON response body.
    4. On failure: delete the key so client can retry.

TTL: 24 hours (86400 seconds) — typical for payment systems.
"""
import json
import logging
from typing import Any, Dict, Optional

from cache.redis_client import get_cache

log = logging.getLogger(__name__)

IDEMPOTENCY_TTL_SECONDS = 86_400   # 24 hours
KEY_PREFIX = "idempotency:"


class DuplicateRequestError(Exception):
    """Raised when an idempotency key collision is detected."""
    def __init__(self, key: str, cached_response: Optional[Dict] = None):
        self.key             = key
        self.cached_response = cached_response
        super().__init__(f"Duplicate request detected for idempotency key: {key!r}")


class IdempotencyManager:
    """Manages idempotency keys using the cache backend."""

    def __init__(self):
        self._cache = None

    def _get_cache(self):
        if self._cache is None:
            self._cache = get_cache()
        return self._cache

    def _make_key(self, idempotency_key: str) -> str:
        return f"{KEY_PREFIX}{idempotency_key}"

    async def check_and_lock(self, idempotency_key: str) -> bool:
        """
        Atomically check-and-lock the idempotency key.

        Returns:
            True  if this is a new (first-time) request. Caller should proceed.
            False if a duplicate. Caller should return cached response.

        Raises:
            DuplicateRequestError: with cached response if available.
        """
        cache = self._get_cache()
        cache_key = self._make_key(idempotency_key)

        # Attempt atomic SET NX EX — only succeeds for first request
        acquired = await cache.set(cache_key, "PROCESSING", ex=IDEMPOTENCY_TTL_SECONDS, nx=True)

        if acquired:
            log.debug("Idempotency: locked key %r (new request).", idempotency_key)
            return True

        # Key exists — check if it has a cached response
        raw = await cache.get(cache_key)
        if raw and raw != "PROCESSING":
            try:
                cached = json.loads(raw)
                raise DuplicateRequestError(idempotency_key, cached_response=cached)
            except json.JSONDecodeError:
                pass

        raise DuplicateRequestError(idempotency_key, cached_response=None)

    async def store_response(self, idempotency_key: str, response_body: Dict) -> None:
        """Store the successful response body, replacing 'PROCESSING' sentinel."""
        cache = self._get_cache()
        cache_key = self._make_key(idempotency_key)
        await cache.set(cache_key, json.dumps(response_body), ex=IDEMPOTENCY_TTL_SECONDS)
        log.debug("Idempotency: stored response for key %r.", idempotency_key)

    async def release(self, idempotency_key: str) -> None:
        """Release (delete) the idempotency key on request failure."""
        cache = self._get_cache()
        await cache.delete(self._make_key(idempotency_key))
        log.debug("Idempotency: released key %r (request failed).", idempotency_key)

    async def get_cached_response(self, idempotency_key: str) -> Optional[Dict]:
        """Retrieve a previously stored response, if any."""
        cache = self._get_cache()
        raw = await cache.get(self._make_key(idempotency_key))
        if raw and raw != "PROCESSING":
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
        return None


# Global singleton
_idempotency_manager: Optional[IdempotencyManager] = None


def get_idempotency_manager() -> IdempotencyManager:
    global _idempotency_manager
    if _idempotency_manager is None:
        _idempotency_manager = IdempotencyManager()
    return _idempotency_manager


"""
Optimistic Concurrency Control (OCC)
=====================================
OCC allows multiple transactions to proceed without locking,
then validates at commit time that no other writer modified
the same resource concurrently.

Strategy:
    1. Read: fetch entity with its current `version` number.
    2. Modify: compute the desired new state locally.
    3. Commit: UPDATE ... WHERE id=:id AND version=:expected_version
    4. If rowcount == 0: another writer won the race — raise conflict.
    5. Retry with truncated exponential backoff + random jitter.

This gives:
    - No shared locks (optimistic = assume no conflict)
    - Conflict detection on commit
    - Automatic retry with backoff
    - Bounded retry count to prevent infinite loops
"""
import asyncio
import functools
import logging
import random
import time
from typing import Any, Callable, Optional, TypeVar

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# --- Default retry configuration ---
OCC_MAX_RETRIES      = 5
OCC_BASE_BACKOFF_MS  = 25    # initial backoff: 25ms
OCC_MAX_BACKOFF_MS   = 2000  # cap backoff at 2s
OCC_JITTER_FRACTION  = 0.25  # ±25% random jitter


class OptimisticLockConflictError(Exception):
    """Raised when a concurrent writer modified the entity before commit."""
    def __init__(self, entity: str, entity_id: str, expected_version: int):
        self.entity           = entity
        self.entity_id        = entity_id
        self.expected_version = expected_version
        super().__init__(
            f"OCC conflict on {entity}[{entity_id}]: "
            f"version {expected_version} was already modified by a concurrent writer."
        )


class MaxRetriesExceededError(Exception):
    """Raised after exhausting all OCC retry attempts."""
    pass


def _compute_backoff(attempt: int, base_ms: int, max_ms: int, jitter: float) -> float:
    """
    Truncated exponential backoff with random jitter.
    backoff = min(base * 2^attempt, max) * (1 ± jitter)
    Returns seconds (float).
    """
    raw_ms    = min(base_ms * (2 ** attempt), max_ms)
    jitter_ms = raw_ms * jitter * (2 * random.random() - 1)
    return max(0, raw_ms + jitter_ms) / 1000.0


def occ_retry(
    max_retries: int = OCC_MAX_RETRIES,
    base_backoff_ms: int = OCC_BASE_BACKOFF_MS,
    max_backoff_ms: int = OCC_MAX_BACKOFF_MS,
    jitter_fraction: float = OCC_JITTER_FRACTION,
):
    """
    Decorator that retries an async function on OptimisticLockConflictError
    using truncated exponential backoff with jitter.

    Usage:
        @occ_retry(max_retries=5)
        async def update_balance(...):
            ...
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except OptimisticLockConflictError as exc:
                    if attempt == max_retries:
                        log.error(
                            "OCC: Max retries (%d) exhausted for %s — giving up. Last conflict: %s",
                            max_retries, func.__name__, exc,
                        )
                        raise MaxRetriesExceededError(
                            f"OCC failed after {max_retries} retries: {exc}"
                        ) from exc

                    delay = _compute_backoff(attempt, base_backoff_ms, max_backoff_ms, jitter_fraction)
                    log.warning(
                        "OCC conflict on attempt %d/%d for %s — retrying in %.1fms",
                        attempt + 1, max_retries, func.__name__, delay * 1000,
                    )
                    await asyncio.sleep(delay)
        return wrapper  # type: ignore
    return decorator


class OCCVersionedEntity:
    """
    A simple in-memory versioned entity for demonstration and testing.
    In production, version checks are done via SQL:
        UPDATE table SET ..., version = version + 1
        WHERE id = :id AND version = :expected_version
    """
    def __init__(self, entity_id: str, initial_value: Any = None):
        self.entity_id  = entity_id
        self.version    = 0
        self.value      = initial_value
        self._lock      = asyncio.Lock()

    async def read(self):
        """Return (value, version) snapshot."""
        return self.value, self.version

    async def compare_and_swap(
        self,
        expected_version: int,
        new_value: Any,
    ) -> int:
        """
        Atomically update if version matches expected_version.
        Returns new version on success.
        Raises OptimisticLockConflictError on version mismatch.
        """
        async with self._lock:
            if self.version != expected_version:
                raise OptimisticLockConflictError(
                    entity="OCCVersionedEntity",
                    entity_id=self.entity_id,
                    expected_version=expected_version,
                )
            self.value    = new_value
            self.version += 1
            return self.version


class OCCBalanceStore:
    """
    Thread-safe in-memory balance store with OCC version tracking.
    Simulates a PostgreSQL versioned row without requiring a DB connection.
    """
    def __init__(self):
        self._entities: dict = {}
        self._global_lock = asyncio.Lock()

    async def get_or_create(self, user_id: str, initial_cents: int = 0) -> OCCVersionedEntity:
        async with self._global_lock:
            if user_id not in self._entities:
                self._entities[user_id] = OCCVersionedEntity(user_id, initial_cents)
        return self._entities[user_id]

    async def apply_delta(
        self,
        user_id: str,
        delta_cents: int,
        expected_version: int,
    ) -> int:
        """
        Apply a balance delta with OCC version check.
        Returns new version on success.
        """
        entity = await self.get_or_create(user_id)
        current_val, _ = await entity.read()
        new_val = current_val + delta_cents
        return await entity.compare_and_swap(expected_version, new_val)


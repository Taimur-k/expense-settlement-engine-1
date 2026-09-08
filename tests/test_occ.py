"""
Test Suite: Optimistic Concurrency Control (OCC)
================================================
Tests conflict detection, retry logic, and backoff behavior.
"""
import asyncio
import pytest
import pytest_asyncio
from core.occ import (
    OCCVersionedEntity, OCCBalanceStore, OptimisticLockConflictError,
    MaxRetriesExceededError, occ_retry, _compute_backoff,
)


# ─── OCCVersionedEntity ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_initial_value():
    """read() returns initial value and version 0."""
    entity = OCCVersionedEntity("test-1", initial_value=100)
    val, ver = await entity.read()
    assert val == 100
    assert ver == 0


@pytest.mark.asyncio
async def test_compare_and_swap_success():
    """CAS with correct version succeeds and increments version."""
    entity = OCCVersionedEntity("test-2", initial_value=0)
    new_ver = await entity.compare_and_swap(expected_version=0, new_value=999)
    assert new_ver == 1
    val, ver = await entity.read()
    assert val == 999
    assert ver == 1


@pytest.mark.asyncio
async def test_compare_and_swap_conflict():
    """CAS with stale version raises OptimisticLockConflictError."""
    entity = OCCVersionedEntity("test-3", initial_value=0)
    # Update once (version becomes 1)
    await entity.compare_and_swap(0, 42)

    # Now try to update with stale version 0
    with pytest.raises(OptimisticLockConflictError):
        await entity.compare_and_swap(0, 99)


@pytest.mark.asyncio
async def test_concurrent_writers():
    """
    Two concurrent writers on the same entity:
    exactly one should succeed, one should fail with conflict.
    """
    entity = OCCVersionedEntity("concurrent-test", initial_value=0)
    successes = 0
    failures  = 0

    async def writer(value: int):
        nonlocal successes, failures
        try:
            await entity.compare_and_swap(0, value)
            successes += 1
        except OptimisticLockConflictError:
            failures += 1

    await asyncio.gather(writer(1), writer(2))
    assert successes == 1
    assert failures  == 1


# ─── OCCBalanceStore ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_balance_store_create():
    """get_or_create initializes a new account with correct value."""
    store  = OCCBalanceStore()
    entity = await store.get_or_create("user-1", initial_cents=5000)
    val, ver = await entity.read()
    assert val == 5000
    assert ver == 0


@pytest.mark.asyncio
async def test_balance_store_apply_delta_success():
    """apply_delta with correct version applies the delta."""
    store = OCCBalanceStore()
    await store.get_or_create("user-2", initial_cents=0)
    new_ver = await store.apply_delta("user-2", delta_cents=500, expected_version=0)
    assert new_ver == 1
    entity = await store.get_or_create("user-2")
    val, _ = await entity.read()
    assert val == 500


@pytest.mark.asyncio
async def test_balance_store_apply_delta_conflict():
    """apply_delta with stale version raises OptimisticLockConflictError."""
    store = OCCBalanceStore()
    await store.get_or_create("user-3", initial_cents=100)
    await store.apply_delta("user-3", 50, expected_version=0)  # version becomes 1
    with pytest.raises(OptimisticLockConflictError):
        await store.apply_delta("user-3", 50, expected_version=0)  # stale


# ─── occ_retry Decorator ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_occ_retry_succeeds_eventually():
    """occ_retry retries on conflict and eventually succeeds."""
    call_count = 0

    @occ_retry(max_retries=4, base_backoff_ms=1, max_backoff_ms=5)
    async def flaky_operation():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise OptimisticLockConflictError("Test", "entity-1", 0)
        return "success"

    result = await flaky_operation()
    assert result == "success"
    assert call_count == 3


@pytest.mark.asyncio
async def test_occ_retry_exhausted_raises():
    """occ_retry raises MaxRetriesExceededError after max retries."""
    @occ_retry(max_retries=2, base_backoff_ms=1, max_backoff_ms=5)
    async def always_conflicts():
        raise OptimisticLockConflictError("Test", "entity-2", 0)

    with pytest.raises(MaxRetriesExceededError):
        await always_conflicts()


@pytest.mark.asyncio
async def test_occ_retry_does_not_retry_non_occ_errors():
    """occ_retry should NOT retry on non-OCC exceptions."""
    call_count = 0

    @occ_retry(max_retries=5)
    async def raises_value_error():
        nonlocal call_count
        call_count += 1
        raise ValueError("Not an OCC error")

    with pytest.raises(ValueError):
        await raises_value_error()

    # Should only be called once (no retry for non-OCC errors)
    assert call_count == 1


# ─── Backoff Calculation ─────────────────────────────────────────────────────

def test_backoff_increases_with_attempt():
    """Backoff should increase (roughly) with each attempt."""
    import random
    random.seed(42)
    delays = [_compute_backoff(i, 25, 2000, 0.0) for i in range(5)]
    # With jitter=0, delays should be purely exponential (no randomness)
    assert delays[0] < delays[1] < delays[2]


def test_backoff_capped_at_max():
    """Backoff should not exceed max_backoff_ms (as seconds)."""
    import random
    random.seed(0)
    delay = _compute_backoff(20, 25, 500, 0.0)
    assert delay <= 0.5 + 0.001  # 500ms + tiny float tolerance


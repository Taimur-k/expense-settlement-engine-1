"""
Test Suite: Message Queue & Outbox
====================================
Tests the in-memory queue, outbox relay, and event publishing.
"""
import asyncio
import pytest
from messaging.queue import InMemoryQueue, get_queue, set_queue
from messaging.outbox import InMemoryOutbox, get_outbox
from messaging.events import EventType


@pytest.mark.asyncio
async def test_queue_publish_and_consume():
    """Published message should be consumable."""
    q   = InMemoryQueue()
    mid = await q.publish("TEST_EVENT", {"data": "hello"})
    msg = await q.consume_one(timeout=0.5)
    assert msg is not None
    assert msg["event_type"] == "TEST_EVENT"
    assert msg["payload"]["data"] == "hello"


@pytest.mark.asyncio
async def test_queue_consume_timeout_returns_none():
    """Empty queue should return None after timeout."""
    q   = InMemoryQueue()
    msg = await q.consume_one(timeout=0.1)
    assert msg is None


@pytest.mark.asyncio
async def test_queue_nack_retry():
    """nack() should re-enqueue the message for retry."""
    q   = InMemoryQueue()
    mid = await q.publish("FAIL_EVENT", {"x": 1})
    msg = await q.consume_one(timeout=0.5)
    await q.nack(msg)  # retry count = 1
    msg2 = await q.consume_one(timeout=0.5)
    assert msg2 is not None  # re-enqueued


@pytest.mark.asyncio
async def test_queue_dlq_after_max_retries():
    """After MAX_RETRIES nacks, message should go to DLQ."""
    from messaging.queue import MAX_RETRIES
    q   = InMemoryQueue()
    mid = await q.publish("BAD_EVENT", {"x": 1})

    msg = await q.consume_one(timeout=0.5)
    # Nack MAX_RETRIES times
    for _ in range(MAX_RETRIES - 1):
        await q.nack(msg)
        msg = await q.consume_one(timeout=0.5)

    await q.nack(msg)  # Final nack -> DLQ
    assert q.dlq_length() == 1


@pytest.mark.asyncio
async def test_outbox_append_and_relay():
    """Appending to outbox and relaying should deliver message to queue."""
    # Use fresh instances to avoid cross-test pollution
    outbox    = InMemoryOutbox()
    queue     = InMemoryQueue()
    set_queue(queue)  # override global

    await outbox.append(
        event_type   = EventType.EXPENSE_CREATED,
        aggregate_id = "expense-123",
        payload      = {"expense_id": "expense-123", "total_cents": 5000},
    )

    assert outbox.pending_count() == 1

    relayed = await outbox.relay_all_pending()
    assert relayed == 1
    assert outbox.pending_count() == 0

    msg = await queue.consume_one(timeout=0.5)
    assert msg is not None
    assert msg["event_type"] == EventType.EXPENSE_CREATED
    assert msg["payload"]["total_cents"] == 5000


@pytest.mark.asyncio
async def test_outbox_relay_all():
    """Multiple outbox events should all be relayed."""
    outbox = InMemoryOutbox()
    queue  = InMemoryQueue()
    set_queue(queue)

    for i in range(5):
        await outbox.append(EventType.EXPENSE_CREATED, f"exp-{i}", {"idx": i})

    relayed = await outbox.relay_all_pending()
    assert relayed == 5

    messages = []
    for _ in range(5):
        msg = await queue.consume_one(timeout=0.5)
        if msg:
            messages.append(msg)

    assert len(messages) == 5


@pytest.mark.asyncio
async def test_outbox_events_tracked():
    """all_events() returns complete event history."""
    outbox = InMemoryOutbox()
    queue  = InMemoryQueue()
    set_queue(queue)

    await outbox.append(EventType.SETTLEMENT_REQUESTED, "grp-1", {"group_id": "grp-1"})
    await outbox.append(EventType.SETTLEMENT_COMPLETED, "grp-1", {"group_id": "grp-1"})

    events = outbox.all_events()
    assert len(events) == 2
    assert events[0]["event_type"] == EventType.SETTLEMENT_REQUESTED
    assert events[1]["event_type"] == EventType.SETTLEMENT_COMPLETED


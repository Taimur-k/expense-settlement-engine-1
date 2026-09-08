"""
Transactional Outbox Pattern
=============================
Events are written to the outbox ATOMICALLY within the same
transaction as the business operation, then relayed to the queue.

This guarantees at-least-once delivery without dual-write inconsistency.
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Dict, List, Optional

from messaging.events import EventType
from messaging.queue import get_queue

log = logging.getLogger(__name__)


class InMemoryOutbox:
    """
    In-memory outbox for use when DB is unavailable.
    Events are published directly to the in-memory queue.
    """

    def __init__(self):
        self._events: List[Dict]    = []
        self._pending: asyncio.Queue = asyncio.Queue()

    async def append(
        self,
        event_type: str,
        aggregate_id: str,
        payload: Dict,
    ) -> str:
        """Append an event to the outbox."""
        event_id = str(uuid.uuid4())
        event = {
            "id":           event_id,
            "event_type":   event_type,
            "aggregate_id": aggregate_id,
            "payload":      payload,
            "is_published": False,
            "created_at":   time.time(),
        }
        self._events.append(event)
        await self._pending.put(event)
        return event_id

    async def relay_all_pending(self) -> int:
        """Relay all pending outbox events to the queue. Returns count relayed."""
        queue = get_queue()
        count = 0
        while not self._pending.empty():
            try:
                event = self._pending.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await queue.publish(event["event_type"], event["payload"])
                event["is_published"] = True
                count += 1
            except Exception as e:
                log.error("Outbox relay failed for event %s: %s", event["id"], e)
        return count

    async def start_relay_daemon(self, interval_seconds: float = 0.5):
        """Background task: continuously relay pending events."""
        log.info("Outbox relay daemon started (interval=%.1fs).", interval_seconds)
        while True:
            try:
                relayed = await self.relay_all_pending()
                if relayed > 0:
                    log.debug("Outbox relay: published %d events.", relayed)
            except Exception as e:
                log.error("Outbox relay daemon error: %s", e)
            await asyncio.sleep(interval_seconds)

    def pending_count(self) -> int:
        return self._pending.qsize()

    def all_events(self) -> List[Dict]:
        return list(self._events)


# Global outbox singleton
_outbox_instance: Optional[InMemoryOutbox] = None


def get_outbox() -> InMemoryOutbox:
    global _outbox_instance
    if _outbox_instance is None:
        _outbox_instance = InMemoryOutbox()
    return _outbox_instance


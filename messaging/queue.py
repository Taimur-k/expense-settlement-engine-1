"""
Message Queue — Redis Streams Publisher & Consumer
===================================================
Producer: XADD to stream  `expense-events`
Consumer: XREADGROUP from consumer group `settlement-workers`
Dead-Letter Queue: stream `expense-events-dlq` for poison pills

Falls back gracefully to in-memory asyncio.Queue when Redis is unavailable.
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Coroutine, Dict, List, Optional

log = logging.getLogger(__name__)

STREAM_NAME    = "expense-events"
DLQ_STREAM     = "expense-events-dlq"
CONSUMER_GROUP = "settlement-workers"
MAX_RETRIES    = 3


class InMemoryQueue:
    """Asyncio-based fallback queue when Redis is not available."""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._dlq: List[Dict]      = []
        self._retry_counts: Dict[str, int] = {}

    async def publish(self, event_type: str, payload: Dict) -> str:
        message_id = str(uuid.uuid4())
        await self._queue.put({
            "id":         message_id,
            "event_type": event_type,
            "payload":    payload,
            "created_at": time.time(),
        })
        log.debug("[InMemoryQueue] Published %s: %s", event_type, message_id)
        return message_id

    async def consume_one(self, timeout: float = 1.0) -> Optional[Dict]:
        try:
            msg = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            return msg
        except asyncio.TimeoutError:
            return None

    async def ack(self, message_id: str) -> None:
        pass  # asyncio.Queue auto-acks on get()

    async def nack(self, message: Dict) -> None:
        mid   = message.get("id", "")
        count = self._retry_counts.get(mid, 0) + 1
        self._retry_counts[mid] = count
        if count >= MAX_RETRIES:
            self._dlq.append(message)
            log.error("[InMemoryQueue] Message %s sent to DLQ after %d retries.", mid, count)
        else:
            await self._queue.put(message)

    def dlq_length(self) -> int:
        return len(self._dlq)

    def qsize(self) -> int:
        return self._queue.qsize()


class RedisStreamQueue:
    """Redis Streams publisher/consumer with consumer group and DLQ support."""

    def __init__(self, redis_client):
        self._redis      = redis_client
        self._consumer_id = f"worker-{uuid.uuid4().hex[:8]}"

    async def ensure_group(self):
        """Create consumer group if it doesn't exist."""
        try:
            await self._redis.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="0", mkstream=True)
        except Exception:
            pass  # Group already exists

    async def publish(self, event_type: str, payload: Dict) -> str:
        data   = {"event_type": event_type, "payload": json.dumps(payload), "ts": str(time.time())}
        msg_id = await self._redis.xadd(STREAM_NAME, data)
        log.debug("[RedisStream] Published %s: %s", event_type, msg_id)
        return msg_id

    async def consume_batch(self, count: int = 10, block_ms: int = 1000) -> List[Dict]:
        """Read a batch of pending messages from the consumer group."""
        results = await self._redis.xreadgroup(
            CONSUMER_GROUP, self._consumer_id,
            {STREAM_NAME: ">"},
            count=count, block=block_ms,
        )
        messages = []
        if results:
            for _, msgs in results:
                for msg_id, fields in msgs:
                    messages.append({
                        "id":         msg_id,
                        "event_type": fields.get("event_type", ""),
                        "payload":    json.loads(fields.get("payload", "{}")),
                        "ts":         fields.get("ts", ""),
                    })
        return messages

    async def ack(self, message_id: str) -> None:
        await self._redis.xack(STREAM_NAME, CONSUMER_GROUP, message_id)

    async def nack(self, message: Dict, retry_count: int) -> None:
        if retry_count >= MAX_RETRIES:
            dlq_data = {
                "original_id": str(message["id"]),
                "event_type":  message["event_type"],
                "payload":     json.dumps(message["payload"]),
                "reason":      "max_retries_exceeded",
                "ts":          str(time.time()),
            }
            await self._redis.xadd(DLQ_STREAM, dlq_data)
            await self.ack(message["id"])
            log.error("[RedisStream] Message %s sent to DLQ.", message["id"])
        else:
            payload = message["payload"]
            payload["_retry_count"] = retry_count + 1
            await self.publish(message["event_type"], payload)
            await self.ack(message["id"])


# Module-level queue singleton (set at startup)
_queue_instance: Optional[Any] = None


def get_queue() -> Any:
    global _queue_instance
    if _queue_instance is None:
        _queue_instance = InMemoryQueue()
        log.info("[Queue] Using InMemoryQueue fallback.")
    return _queue_instance


def set_queue(q):
    global _queue_instance
    _queue_instance = q


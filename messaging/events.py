"""
Event Schemas for the Transactional Outbox & Message Queue
==========================================================
All events are serialized as JSON and published to Redis Streams.
"""
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class EventType(str, Enum):
    EXPENSE_CREATED      = "EXPENSE_CREATED"
    SETTLEMENT_REQUESTED = "SETTLEMENT_REQUESTED"
    LEDGER_POSTED        = "LEDGER_POSTED"
    SETTLEMENT_COMPLETED = "SETTLEMENT_COMPLETED"
    SETTLEMENT_FAILED    = "SETTLEMENT_FAILED"


@dataclass
class BaseEvent:
    event_id:    str   = field(default_factory=lambda: str(uuid.uuid4()))
    event_type:  str   = EventType.EXPENSE_CREATED
    created_at:  float = field(default_factory=time.time)
    version:     int   = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "BaseEvent":
        return cls(**json.loads(data))


@dataclass
class ExpenseCreatedEvent(BaseEvent):
    event_type:      str             = EventType.EXPENSE_CREATED
    expense_id:      str             = ""
    group_id:        str             = ""
    payer_id:        str             = ""
    total_cents:     int             = 0
    title:           str             = ""
    splits:          List[Dict]      = field(default_factory=list)
    idempotency_key: Optional[str]   = None


@dataclass
class SettlementRequestedEvent(BaseEvent):
    event_type:   str           = EventType.SETTLEMENT_REQUESTED
    group_id:     str           = ""
    requested_by: str           = ""
    net_balances: Dict[str, int] = field(default_factory=dict)


@dataclass
class LedgerPostedEvent(BaseEvent):
    event_type:  str = EventType.LEDGER_POSTED
    journal_id:  str = ""
    expense_id:  str = ""
    group_id:    str = ""
    total_cents: int = 0


@dataclass
class SettlementCompletedEvent(BaseEvent):
    event_type:          str = EventType.SETTLEMENT_COMPLETED
    group_id:            str = ""
    settlement_count:    int = 0
    total_settled_cents: int = 0
    algorithm:           str = "MaxHeap-O(NlogN)"
    checksum:            str = ""


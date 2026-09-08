"""
In-Memory Store (Shared State)
================================
Single source of truth for the application's in-memory persistence layer.
Used when PostgreSQL is unavailable (development / testing).
All access is async-safe using asyncio.Lock.
"""
import asyncio
import uuid
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class UserRecord:
    id:         str
    name:       str
    email:      str
    version:    int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class GroupRecord:
    id:         str
    name:       str
    version:    int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    member_ids: List[str] = field(default_factory=list)


@dataclass
class ExpenseRecord:
    id:              str
    group_id:        str
    payer_id:        str
    title:           str
    total_cents:     int
    split_type:      str
    status:          str = "PENDING"
    idempotency_key: Optional[str] = None
    version:         int = 0
    journal_id:      Optional[str] = None
    created_at:      datetime = field(default_factory=datetime.utcnow)
    splits:          List[Dict] = field(default_factory=list)


@dataclass
class SettlementRecord:
    id:           str
    group_id:     str
    payer_id:     str
    payee_id:     str
    amount_cents: int
    is_executed:  bool = False
    algorithm:    str = "MaxHeap-O(NlogN)"
    checksum:     str = ""
    created_at:   datetime = field(default_factory=datetime.utcnow)
    executed_at:  Optional[datetime] = None


class InMemoryStore:
    """Thread-safe in-memory data store for all entities."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.users:       Dict[str, UserRecord]       = {}
        self.groups:      Dict[str, GroupRecord]      = {}
        self.expenses:    Dict[str, ExpenseRecord]    = {}
        self.settlements: Dict[str, SettlementRecord] = {}
        self.idempotency_keys: Dict[str, str]         = {}  # key -> expense_id

    # ---- Users ----
    async def create_user(self, name: str, email: str) -> UserRecord:
        async with self._lock:
            # Check uniqueness
            for u in self.users.values():
                if u.email == email:
                    raise ValueError(f"Email {email!r} already exists.")
            user = UserRecord(id=str(uuid.uuid4()), name=name, email=email)
            self.users[user.id] = user
            return user

    async def get_user(self, user_id: str) -> Optional[UserRecord]:
        return self.users.get(user_id)

    async def list_users(self) -> List[UserRecord]:
        return list(self.users.values())

    # ---- Groups ----
    async def create_group(self, name: str) -> GroupRecord:
        async with self._lock:
            group = GroupRecord(id=str(uuid.uuid4()), name=name)
            self.groups[group.id] = group
            return group

    async def get_group(self, group_id: str) -> Optional[GroupRecord]:
        return self.groups.get(group_id)

    async def add_member(self, group_id: str, user_id: str) -> bool:
        async with self._lock:
            grp = self.groups.get(group_id)
            if not grp:
                return False
            if user_id not in grp.member_ids:
                grp.member_ids.append(user_id)
                grp.version += 1
            return True

    # ---- Expenses ----
    async def create_expense(
        self,
        group_id: str,
        payer_id: str,
        title: str,
        total_cents: int,
        split_type: str,
        splits: List[Dict],
        idempotency_key: Optional[str] = None,
    ) -> ExpenseRecord:
        async with self._lock:
            # Idempotency check
            if idempotency_key and idempotency_key in self.idempotency_keys:
                existing_id = self.idempotency_keys[idempotency_key]
                existing = self.expenses.get(existing_id)
                if existing:
                    return existing

            expense = ExpenseRecord(
                id              = str(uuid.uuid4()),
                group_id        = group_id,
                payer_id        = payer_id,
                title           = title,
                total_cents     = total_cents,
                split_type      = split_type,
                splits          = splits,
                idempotency_key = idempotency_key,
                status          = "POSTED",
            )
            self.expenses[expense.id] = expense
            if idempotency_key:
                self.idempotency_keys[idempotency_key] = expense.id
            return expense

    async def get_expenses_for_group(self, group_id: str) -> List[ExpenseRecord]:
        return [e for e in self.expenses.values() if e.group_id == group_id]

    # ---- Settlements ----
    async def save_settlements(self, records: List[SettlementRecord]) -> None:
        async with self._lock:
            for s in records:
                self.settlements[s.id] = s

    async def get_settlements_for_group(self, group_id: str) -> List[SettlementRecord]:
        return [s for s in self.settlements.values() if s.group_id == group_id]

    async def mark_settlement_executed(self, settlement_id: str) -> None:
        async with self._lock:
            s = self.settlements.get(settlement_id)
            if s:
                s.is_executed = True
                s.executed_at = datetime.utcnow()


# Global singleton
_store: Optional[InMemoryStore] = None


def get_store() -> InMemoryStore:
    global _store
    if _store is None:
        _store = InMemoryStore()
    return _store


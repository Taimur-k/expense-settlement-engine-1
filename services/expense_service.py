"""
Expense Service
================
Handles the full lifecycle of expense creation:

1. Validate all participants belong to the group.
2. Compute fair splits (equal / exact / percentage).
3. Atomically persist expense + splits (OCC-protected).
4. Post double-entry journal entries via LedgerEngine.
5. Emit EXPENSE_CREATED event to Transactional Outbox.

All monetary math is done in integer cents. No floating-point.
"""
import logging
import uuid
from typing import Dict, List, Optional

from core.debt_simplifier import DebtSimplifier
from core.ledger_engine import LedgerEngine
from core.occ import occ_retry, OptimisticLockConflictError
from database.store import get_store, ExpenseRecord, SettlementRecord
from messaging.outbox import get_outbox
from messaging.events import EventType

log = logging.getLogger(__name__)

_ledger   = LedgerEngine()
_simplifier = DebtSimplifier()


def _split_equal(total_cents: int, user_ids: List[str]) -> List[Dict]:
    """Split total evenly. Remainder goes to the first user (banker's rounding avoided)."""
    n = len(user_ids)
    base = total_cents // n
    remainder = total_cents % n
    splits = []
    for i, uid in enumerate(user_ids):
        amt = base + (1 if i < remainder else 0)
        splits.append({"user_id": uid, "amount_cents": amt})
    return splits


def _split_exact(splits_input: List[Dict], total_cents: int) -> List[Dict]:
    """Validate exact split amounts sum to total."""
    given = sum(s["amount_cents"] for s in splits_input)
    if given != total_cents:
        raise ValueError(
            f"Exact splits sum ({given}) != total ({total_cents}). "
            f"Difference: {given - total_cents} cents."
        )
    return splits_input


def _split_percentage(percentages: List[Dict], total_cents: int) -> List[Dict]:
    """Convert percentages to cents. Last user absorbs rounding remainder."""
    total_pct = sum(p["percentage"] for p in percentages)
    if abs(total_pct - 100.0) > 0.01:
        raise ValueError(f"Percentages must sum to 100, got {total_pct}")
    splits = []
    allocated = 0
    for i, p in enumerate(percentages):
        if i == len(percentages) - 1:
            amt = total_cents - allocated
        else:
            amt = int(total_cents * p["percentage"] / 100)
        allocated += amt
        splits.append({"user_id": p["user_id"], "amount_cents": amt})
    return splits


class ExpenseService:
    """Service layer for expense operations."""

    @occ_retry(max_retries=5)
    async def create_expense(
        self,
        group_id:        str,
        payer_id:        str,
        title:           str,
        total_cents:     int,
        split_type:      str      = "EQUAL",
        splits_input:    Optional[List[Dict]] = None,
        idempotency_key: Optional[str]  = None,
    ) -> ExpenseRecord:
        """
        Create an expense with double-entry ledger posting and outbox event.

        Args:
            group_id:        Group in which the expense occurred.
            payer_id:        User who paid the full amount.
            title:           Human-readable description.
            total_cents:     Total amount in cents (must be >= 1).
            split_type:      'EQUAL' | 'EXACT' | 'PERCENTAGE'
            splits_input:    Required for EXACT/PERCENTAGE splits.
            idempotency_key: Client-provided unique key to prevent duplicates.

        Returns:
            Created ExpenseRecord.
        """
        if total_cents < 1:
            raise ValueError(f"Expense total must be >= 1 cent, got {total_cents}.")

        store = get_store()

        # Validate group exists
        group = await store.get_group(group_id)
        if not group:
            raise ValueError(f"Group {group_id!r} not found.")

        member_ids = group.member_ids
        if not member_ids:
            raise ValueError(f"Group {group_id!r} has no members.")

        if payer_id not in member_ids:
            raise ValueError(f"Payer {payer_id!r} is not a member of group {group_id!r}.")

        # Compute splits
        if split_type == "EQUAL":
            splits = _split_equal(total_cents, member_ids)
        elif split_type == "EXACT":
            if not splits_input:
                raise ValueError("EXACT split requires splits_input.")
            splits = _split_exact(splits_input, total_cents)
        elif split_type == "PERCENTAGE":
            if not splits_input:
                raise ValueError("PERCENTAGE split requires splits_input.")
            splits = _split_percentage(splits_input, total_cents)
        else:
            raise ValueError(f"Unknown split_type: {split_type!r}")

        # Post double-entry ledger journal
        journal = _ledger.post_expense_journal(
            payer_id    = payer_id,
            splits      = splits,
            description = title,
        )

        # Persist expense
        expense = await store.create_expense(
            group_id        = group_id,
            payer_id        = payer_id,
            title           = title,
            total_cents     = total_cents,
            split_type      = split_type,
            splits          = splits,
            idempotency_key = idempotency_key,
        )
        expense.journal_id = journal.journal_id

        # Publish to outbox
        outbox = get_outbox()
        await outbox.append(
            event_type   = EventType.EXPENSE_CREATED,
            aggregate_id = expense.id,
            payload      = {
                "expense_id":       expense.id,
                "group_id":         group_id,
                "payer_id":         payer_id,
                "total_cents":      total_cents,
                "title":            title,
                "splits":           splits,
                "idempotency_key":  idempotency_key,
            },
        )

        log.info(
            "ExpenseService: Created expense %s in group %s — %s (%d cents, %s split).",
            expense.id, group_id, title, total_cents, split_type,
        )
        return expense

    async def get_group_net_balances(self, group_id: str) -> Dict[str, int]:
        """
        Compute net balances for all members of a group.
        Returns {user_id: net_balance_cents}.
        Positive => creditor (is owed money).
        Negative => debtor (owes money).
        """
        store    = get_store()
        expenses = await store.get_expenses_for_group(group_id)

        raw_txs = [
            {"payer_id": e.payer_id, "splits": e.splits}
            for e in expenses
        ]
        return _simplifier.compute_net_balances(raw_txs)

    async def get_simplified_debts(self, group_id: str):
        """Run the O(N log N) algorithm and return simplified debt graph."""
        net = await self.get_group_net_balances(group_id)
        store    = get_store()
        expenses = await store.get_expenses_for_group(group_id)
        # Count pairwise debts: each expense contributes (num_splits - 1) debt edges
        pairwise_count = sum(
            max(0, len(e.splits) - 1) for e in expenses
        )
        return _simplifier.simplify(net, original_tx_count=max(pairwise_count, len(expenses)))



_expense_service: Optional[ExpenseService] = None


def get_expense_service() -> ExpenseService:
    global _expense_service
    if _expense_service is None:
        _expense_service = ExpenseService()
    return _expense_service

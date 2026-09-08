"""
Settlement Worker
==================
Async daemon that:
1. Consumes SETTLEMENT_REQUESTED events from the queue.
2. Runs the O(N log N) Max-Heap debt simplification algorithm.
3. Posts double-entry settlement journals via LedgerEngine.
4. Saves the simplified SettlementPlan to the store.
5. Invalidates the group's cached balance from Redis/InMemoryCache.
6. Emits SETTLEMENT_COMPLETED event.

Runs as a background asyncio task (started by the FastAPI lifespan).
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from core.debt_simplifier import DebtSimplifier, ZeroSumViolationError
from core.ledger_engine import LedgerEngine
from database.store import get_store, SettlementRecord
from messaging.outbox import get_outbox
from messaging.queue import get_queue
from messaging.events import EventType
from cache.redis_client import get_cache

log = logging.getLogger(__name__)

_simplifier = DebtSimplifier()
_ledger     = LedgerEngine()


class SettlementWorker:
    """
    Async worker that processes settlement requests from the message queue.
    """

    def __init__(self):
        self._running = False
        self._task:   Optional[asyncio.Task] = None
        self._processed = 0
        self._failed    = 0

    async def start(self):
        """Start the worker daemon."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        log.info("SettlementWorker started.")

    async def stop(self):
        """Gracefully stop the worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("SettlementWorker stopped. Processed=%d, Failed=%d.",
                 self._processed, self._failed)

    async def _run_loop(self):
        """Main consume loop."""
        queue = get_queue()
        while self._running:
            try:
                msg = await queue.consume_one(timeout=0.5)
                if msg is None:
                    continue

                event_type = msg.get("event_type", "")
                if event_type == EventType.SETTLEMENT_REQUESTED:
                    await self._handle_settlement(msg)
                elif event_type == EventType.EXPENSE_CREATED:
                    await self._handle_expense_created(msg)
                # Else: ignore other event types in worker
                self._processed += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._failed += 1
                log.error("SettlementWorker: unhandled error: %s", e, exc_info=True)
                await asyncio.sleep(1.0)

    async def _handle_expense_created(self, msg: Dict):
        """Log expense creation events (ledger already posted by ExpenseService)."""
        payload = msg.get("payload", {})
        log.info(
            "Worker: EXPENSE_CREATED — expense=%s group=%s amount=%d cents.",
            payload.get("expense_id"), payload.get("group_id"), payload.get("total_cents", 0),
        )

    async def _handle_settlement(self, msg: Dict):
        """
        Core settlement handler:
        1. Compute net balances for the group.
        2. Run O(N log N) simplification.
        3. Post settlement journals.
        4. Save plan to store.
        5. Invalidate cache.
        6. Emit completion event.
        """
        payload  = msg.get("payload", {})
        group_id = payload.get("group_id", "")

        if not group_id:
            log.error("Worker: settlement request missing group_id.")
            return

        log.info("Worker: processing settlement for group %s.", group_id)

        store     = get_store()
        expenses  = await store.get_expenses_for_group(group_id)

        if not expenses:
            log.info("Worker: no expenses for group %s — nothing to settle.", group_id)
            return

        raw_txs = [{"payer_id": e.payer_id, "splits": e.splits} for e in expenses]
        net_balances = _simplifier.compute_net_balances(raw_txs)

        try:
            result = _simplifier.simplify(net_balances, original_tx_count=len(expenses))
        except ZeroSumViolationError as e:
            log.error("Worker: ZeroSumViolation for group %s: %s", group_id, e)
            return

        # Post settlement journals and save records
        settlement_records = []
        total_settled = 0

        for s in result.settlements:
            journal = _ledger.post_settlement_journal(
                payer_id     = s.payer_id,
                payee_id     = s.payee_id,
                amount_cents = s.amount_cents,
                description  = f"Settlement: {s.payer_id} -> {s.payee_id}",
            )
            rec = SettlementRecord(
                id           = str(uuid.uuid4()),
                group_id     = group_id,
                payer_id     = s.payer_id,
                payee_id     = s.payee_id,
                amount_cents = s.amount_cents,
                is_executed  = True,
                algorithm    = "MaxHeap-O(NlogN)",
                checksum     = result.checksum,
                executed_at  = datetime.utcnow(),
            )
            settlement_records.append(rec)
            total_settled += s.amount_cents

        await store.save_settlements(settlement_records)

        # Invalidate group balance cache
        cache = get_cache()
        await cache.delete(f"group:{group_id}:balances")
        await cache.delete(f"group:{group_id}:debts")

        # Emit completion event
        outbox = get_outbox()
        await outbox.append(
            event_type   = EventType.SETTLEMENT_COMPLETED,
            aggregate_id = group_id,
            payload      = {
                "group_id":             group_id,
                "settlement_count":     len(result.settlements),
                "total_settled_cents":  total_settled,
                "algorithm":            "MaxHeap-O(NlogN)",
                "reduction_ratio":      result.reduction_ratio,
                "checksum":             result.checksum,
                "execution_time_us":    result.execution_time_us,
            },
        )

        log.info(
            "Worker: Settlement complete for group %s. "
            "%d transactions (reduced from %d, ratio=%.1f%%). "
            "Total settled: %d cents. Time: %.1fµs.",
            group_id,
            result.simplified_transaction_count,
            result.original_transaction_count,
            result.reduction_ratio * 100,
            total_settled,
            result.execution_time_us,
        )

    @property
    def stats(self) -> Dict:
        return {"processed": self._processed, "failed": self._failed, "running": self._running}


# Global singleton
_worker: Optional[SettlementWorker] = None


def get_worker() -> SettlementWorker:
    global _worker
    if _worker is None:
        _worker = SettlementWorker()
    return _worker


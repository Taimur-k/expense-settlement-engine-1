"""
Ledger Audit Routes
====================
GET /api/v1/ledger/audit   - Full ledger audit report
GET /api/v1/health         - System health check
"""
from fastapi import APIRouter

from api.schemas.schemas import LedgerAuditResponse, HealthResponse
from core.ledger_engine import LedgerEngine
from worker.worker import get_worker
from messaging.queue import get_queue
from cache.redis_client import get_cache

router = APIRouter(prefix="/api/v1", tags=["Ledger & Health"])

# Shared ledger instance (same as used by expense_service)
# In production this would be injected via DI
_ledger = LedgerEngine()


def get_shared_ledger() -> LedgerEngine:
    """
    Returns the same LedgerEngine instance used by services.
    We import from expense_service to get the shared singleton.
    """
    from services.expense_service import _ledger as svc_ledger
    return svc_ledger


@router.get("/ledger/audit", response_model=LedgerAuditResponse)
async def audit_ledger():
    """
    Run a full double-entry ledger audit:
    - Verify total debits == total credits
    - Verify SHA-256 hash chain integrity of all entries
    - Report any tamper-detection failures
    """
    ledger = get_shared_ledger()
    report = ledger.audit_report()
    return LedgerAuditResponse(
        total_entries         = report["total_entries"],
        total_journals        = report["total_journals"],
        total_debits_cents    = report["total_debits_cents"],
        total_credits_cents   = report["total_credits_cents"],
        total_debits_dollars  = round(report["total_debits_cents"] / 100, 2),
        total_credits_dollars = round(report["total_credits_cents"] / 100, 2),
        is_balanced           = report["is_balanced"],
        chain_integrity_ok    = report["chain_integrity_ok"],
        chain_errors          = report["chain_errors"],
    )


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """System health check: worker stats, queue size, cache type."""
    worker = get_worker()
    queue  = get_queue()
    cache  = get_cache()

    queue_size = None
    try:
        queue_size = queue.qsize() if hasattr(queue, "qsize") else None
    except Exception:
        pass

    cache_type = type(cache).__name__

    return HealthResponse(
        status       = "ok",
        version      = "1.0.0",
        worker_stats = worker.stats,
        queue_size   = queue_size,
        cache_type   = cache_type,
    )


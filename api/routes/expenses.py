"""
Expenses API Routes
====================
POST /api/v1/expenses              - Create expense (idempotent, OCC-protected)
GET  /api/v1/groups/{id}/expenses  - List expenses for a group
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, status

from api.schemas.schemas import (
    CreateExpenseRequest, ExpenseResponse, ExpenseSplitResponse,
)
from services.expense_service import get_expense_service
from cache.idempotency import get_idempotency_manager, DuplicateRequestError

router = APIRouter(prefix="/api/v1", tags=["Expenses"])


def _fmt_expense(e) -> ExpenseResponse:
    splits = [
        ExpenseSplitResponse(user_id=s["user_id"], amount_cents=s["amount_cents"])
        for s in (e.splits or [])
    ]
    return ExpenseResponse(
        id              = e.id,
        group_id        = e.group_id,
        payer_id        = e.payer_id,
        title           = e.title,
        total_cents     = e.total_cents,
        total_dollars   = round(e.total_cents / 100, 2),
        split_type      = e.split_type,
        status          = e.status,
        splits          = splits,
        idempotency_key = e.idempotency_key,
        journal_id      = e.journal_id,
        created_at      = e.created_at.isoformat() if isinstance(e.created_at, datetime) else str(e.created_at),
    )


@router.post(
    "/groups/{group_id}/expenses",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Expense",
    description=(
        "Record a new shared expense. "
        "Protected by Optimistic Concurrency Control (OCC) with auto-retry. "
        "Idempotent: supply X-Idempotency-Key header to prevent duplicates."
    ),
)
async def create_expense(
    group_id: str,
    body:     CreateExpenseRequest,
    x_idempotency_key: Optional[str] = Header(default=None),
):
    """
    Create a shared expense with double-entry ledger posting.
    Idempotency key may be provided via X-Idempotency-Key header
    or in the request body's idempotency_key field.
    """
    # Resolve idempotency key (header takes precedence)
    idem_key = x_idempotency_key or body.idempotency_key

    idempotency_mgr = get_idempotency_manager()

    # Check for duplicate (idempotency gate)
    if idem_key:
        try:
            await idempotency_mgr.check_and_lock(idem_key)
        except DuplicateRequestError as e:
            if e.cached_response:
                # Return the original response
                return ExpenseResponse(**e.cached_response)
            raise HTTPException(
                status_code=409,
                detail=f"Duplicate request for idempotency key {idem_key!r}. "
                       "Previous request is still processing.",
            )

    svc = get_expense_service()

    # Build splits_input from body
    splits_input = None
    if body.splits:
        splits_input = [s.model_dump(exclude_none=True) for s in body.splits]

    try:
        expense = await svc.create_expense(
            group_id        = group_id,
            payer_id        = body.payer_id,
            title           = body.title,
            total_cents     = body.total_cents,
            split_type      = body.split_type,
            splits_input    = splits_input,
            idempotency_key = idem_key,
        )
    except (ValueError, Exception) as e:
        # Release idempotency lock on failure so client can retry
        if idem_key:
            await idempotency_mgr.release(idem_key)
        raise HTTPException(status_code=400, detail=str(e))

    response = _fmt_expense(expense)

    # Cache the response for idempotency
    if idem_key:
        await idempotency_mgr.store_response(idem_key, response.model_dump())

    return response


@router.get("/groups/{group_id}/expenses", response_model=List[ExpenseResponse])
async def list_expenses(group_id: str):
    """List all expenses for a group."""
    from database.store import get_store
    store    = get_store()
    expenses = await store.get_expenses_for_group(group_id)
    return [_fmt_expense(e) for e in expenses]


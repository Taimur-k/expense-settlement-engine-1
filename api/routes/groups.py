"""
Groups API Routes
=================
POST /api/v1/groups                      - Create group
GET  /api/v1/groups/{id}                 - Get group
POST /api/v1/groups/{id}/members         - Add member
GET  /api/v1/groups/{id}/members         - List members
GET  /api/v1/groups/{id}/balances        - Net balances (cached)
GET  /api/v1/groups/{id}/simplify        - O(N log N) simplified debts
POST /api/v1/groups/{id}/settle          - Trigger settlement
"""
import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status, Request

from api.schemas.schemas import (
    CreateGroupRequest, GroupResponse, AddMemberRequest, UserResponse,
    GroupBalancesResponse, BalanceEntry, SimplifyResponse, SettlementItem,
    SettleRequest, SettleResponse,
)
from services.group_service import get_group_service
from services.expense_service import get_expense_service
from messaging.outbox import get_outbox
from messaging.events import EventType
from cache.redis_client import get_cache

router = APIRouter(prefix="/api/v1/groups", tags=["Groups"])


def _fmt_group(g) -> GroupResponse:
    return GroupResponse(
        id         = g.id,
        name       = g.name,
        version    = g.version,
        member_ids = g.member_ids,
        created_at = g.created_at.isoformat() if isinstance(g.created_at, datetime) else str(g.created_at),
    )


def _fmt_user(u) -> UserResponse:
    return UserResponse(
        id         = u.id,
        name       = u.name,
        email      = u.email,
        version    = u.version,
        created_at = u.created_at.isoformat() if isinstance(u.created_at, datetime) else str(u.created_at),
    )


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(body: CreateGroupRequest):
    svc   = get_group_service()
    group = await svc.create_group(name=body.name)
    return _fmt_group(group)


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(group_id: str):
    svc   = get_group_service()
    group = await svc.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Group {group_id!r} not found.")
    return _fmt_group(group)


@router.post("/{group_id}/members", status_code=status.HTTP_200_OK)
async def add_member(group_id: str, body: AddMemberRequest):
    svc = get_group_service()
    try:
        await svc.add_member(group_id, body.user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"message": f"User {body.user_id!r} added to group {group_id!r}."}


@router.get("/{group_id}/members", response_model=List[UserResponse])
async def list_members(group_id: str):
    svc     = get_group_service()
    members = await svc.get_members(group_id)
    return [_fmt_user(m) for m in members]


@router.get("/{group_id}/balances", response_model=GroupBalancesResponse)
async def get_balances(group_id: str):
    """
    Return net balances for all group members.
    Positive = creditor (is owed money), Negative = debtor (owes money).
    Results are cached in Redis for 60 seconds.
    """
    cache     = get_cache()
    cache_key = f"group:{group_id}:balances"
    cached    = await cache.get(cache_key)

    if cached:
        data = json.loads(cached)
        data["cached"] = True
        return GroupBalancesResponse(**data)

    svc  = get_expense_service()
    grp_svc = get_group_service()

    group = await grp_svc.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Group {group_id!r} not found.")

    net = await svc.get_group_net_balances(group_id)

    entries = []
    for uid, cents in net.items():
        role = "CREDITOR" if cents > 0 else ("DEBTOR" if cents < 0 else "SETTLED")
        entries.append(BalanceEntry(
            user_id     = uid,
            net_cents   = cents,
            net_dollars = round(cents / 100, 2),
            role        = role,
        ))

    result = GroupBalancesResponse(
        group_id        = group_id,
        member_balances = entries,
        total_creditors = sum(1 for e in entries if e.role == "CREDITOR"),
        total_debtors   = sum(1 for e in entries if e.role == "DEBTOR"),
        is_settled      = all(e.net_cents == 0 for e in entries),
        cached          = False,
    )

    # Cache for 60 seconds
    await cache.set(cache_key, result.model_dump_json(), ex=60)
    return result


@router.get("/{group_id}/simplify", response_model=SimplifyResponse)
async def simplify_debts(group_id: str):
    """
    Run the O(N log N) Max-Heap debt simplification algorithm.
    Returns the minimal set of transactions to settle the group.
    """
    svc   = get_expense_service()
    grp_svc = get_group_service()

    group = await grp_svc.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Group {group_id!r} not found.")

    result = await svc.get_simplified_debts(group_id)

    items = [
        SettlementItem(
            payer_id      = s.payer_id,
            payee_id      = s.payee_id,
            amount_cents  = s.amount_cents,
            amount_dollars = round(s.amount_cents / 100, 2),
        )
        for s in result.settlements
    ]

    return SimplifyResponse(
        group_id                     = group_id,
        settlements                  = items,
        original_transaction_count   = result.original_transaction_count,
        simplified_transaction_count = result.simplified_transaction_count,
        reduction_ratio_pct          = round(result.reduction_ratio * 100, 2),
        algorithm                    = result.algorithm_complexity,
        execution_time_us            = round(result.execution_time_us, 3),
        checksum                     = result.checksum,
    )


@router.post("/{group_id}/settle", response_model=SettleResponse)
async def settle_group(group_id: str, body: SettleRequest):
    """
    Trigger settlement for the group.
    Publishes SETTLEMENT_REQUESTED to the outbox → queue → worker.
    """
    grp_svc = get_group_service()
    group   = await grp_svc.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Group {group_id!r} not found.")

    svc    = get_expense_service()
    net    = await svc.get_group_net_balances(group_id)

    outbox = get_outbox()
    await outbox.append(
        event_type   = EventType.SETTLEMENT_REQUESTED,
        aggregate_id = group_id,
        payload      = {
            "group_id":     group_id,
            "requested_by": body.requested_by,
            "net_balances": net,
        },
    )

    return SettleResponse(
        group_id = group_id,
        status   = "QUEUED",
        message  = "Settlement request queued. Worker will process asynchronously.",
    )


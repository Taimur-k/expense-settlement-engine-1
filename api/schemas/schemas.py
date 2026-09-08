"""
Pydantic Request/Response Schemas
===================================
All API models with validation, examples, and documentation.
"""
from __future__ import annotations
from typing import Dict, List, Optional
from pydantic import BaseModel, EmailStr, Field, field_validator


# ─── User Schemas ─────────────────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    name:  str = Field(..., min_length=1, max_length=255, examples=["Alice"])
    email: str = Field(..., examples=["alice@example.com"])

    @field_validator("email")
    @classmethod
    def email_must_contain_at(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("Invalid email address.")
        return v.lower().strip()


class UserResponse(BaseModel):
    id:         str
    name:       str
    email:      str
    version:    int
    created_at: str

    model_config = {"from_attributes": True}


# ─── Group Schemas ────────────────────────────────────────────────────────────

class CreateGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["Weekend Trip"])


class AddMemberRequest(BaseModel):
    user_id: str = Field(..., examples=["user-uuid-here"])


class GroupResponse(BaseModel):
    id:         str
    name:       str
    version:    int
    member_ids: List[str]
    created_at: str

    model_config = {"from_attributes": True}


# ─── Expense Schemas ──────────────────────────────────────────────────────────

class SplitInput(BaseModel):
    user_id:      str
    amount_cents: Optional[int]  = None   # For EXACT splits
    percentage:   Optional[float] = None  # For PERCENTAGE splits


class CreateExpenseRequest(BaseModel):
    payer_id:        str         = Field(..., examples=["user-uuid"])
    title:           str         = Field(..., min_length=1, max_length=500, examples=["Dinner at La Piazza"])
    total_cents:     int         = Field(..., ge=1, examples=[6000])
    split_type:      str         = Field(default="EQUAL", examples=["EQUAL"])
    splits:          Optional[List[SplitInput]] = None
    idempotency_key: Optional[str] = Field(default=None, examples=["client-uuid-123"])

    @field_validator("split_type")
    @classmethod
    def validate_split_type(cls, v: str) -> str:
        allowed = {"EQUAL", "EXACT", "PERCENTAGE"}
        if v.upper() not in allowed:
            raise ValueError(f"split_type must be one of {allowed}.")
        return v.upper()

    @field_validator("total_cents")
    @classmethod
    def total_must_be_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("total_cents must be >= 1.")
        return v


class ExpenseSplitResponse(BaseModel):
    user_id:      str
    amount_cents: int


class ExpenseResponse(BaseModel):
    id:              str
    group_id:        str
    payer_id:        str
    title:           str
    total_cents:     float  # Return as dollars for display (cents internally)
    total_dollars:   float
    split_type:      str
    status:          str
    splits:          List[ExpenseSplitResponse]
    idempotency_key: Optional[str]
    journal_id:      Optional[str]
    created_at:      str


# ─── Balance & Settlement Schemas ─────────────────────────────────────────────

class BalanceEntry(BaseModel):
    user_id:        str
    net_cents:      int
    net_dollars:    float
    role:           str   # "CREDITOR" | "DEBTOR" | "SETTLED"


class GroupBalancesResponse(BaseModel):
    group_id:        str
    member_balances: List[BalanceEntry]
    total_creditors: int
    total_debtors:   int
    is_settled:      bool
    cached:          bool = False


class SettlementItem(BaseModel):
    payer_id:     str
    payee_id:     str
    amount_cents: int
    amount_dollars: float


class SimplifyResponse(BaseModel):
    group_id:                     str
    settlements:                  List[SettlementItem]
    original_transaction_count:   int
    simplified_transaction_count: int
    reduction_ratio_pct:          float
    algorithm:                    str
    execution_time_us:            float
    checksum:                     str


class SettleRequest(BaseModel):
    requested_by: str = Field(..., examples=["user-uuid"])


class SettleResponse(BaseModel):
    group_id:        str
    status:          str   # "QUEUED" | "COMPLETED"
    message:         str
    settlements:     List[SettlementItem] = []
    checksum:        str = ""


# ─── Ledger / Audit Schemas ───────────────────────────────────────────────────

class LedgerAuditResponse(BaseModel):
    total_entries:        int
    total_journals:       int
    total_debits_cents:   int
    total_credits_cents:  int
    total_debits_dollars: float
    total_credits_dollars: float
    is_balanced:          bool
    chain_integrity_ok:   bool
    chain_errors:         List[str]


# ─── Generic Response ────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    error:   str
    detail:  Optional[str] = None
    code:    int = 400


class HealthResponse(BaseModel):
    status:  str = "ok"
    version: str = "1.0.0"
    worker_stats: Optional[Dict] = None
    queue_size:   Optional[int]  = None
    cache_type:   str            = "InMemoryCache"


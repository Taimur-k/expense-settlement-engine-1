"""
Double-Entry Accounting Ledger Engine
======================================
Every financial event produces balanced journal entries:
    sum(Debits) == sum(Credits)  [fundamental accounting identity]

Account Types:
    USER_WALLET       - Each user's asset account
    EXPENSE_CLEARING  - Expense clearing account (transient)
    SETTLEMENT_CLEARING - Settlement clearing account
    INCOME_CONTROL    - Group income control account

Immutability:
    Ledger entries are append-only and cryptographically hash-chained
    using SHA-256 to guarantee tamper detection.
"""
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class AccountType(str, Enum):
    USER_WALLET         = "USER_WALLET"
    EXPENSE_CLEARING    = "EXPENSE_CLEARING"
    SETTLEMENT_CLEARING = "SETTLEMENT_CLEARING"
    INCOME_CONTROL      = "INCOME_CONTROL"


class EntryType(str, Enum):
    DEBIT  = "DEBIT"   # Increases asset accounts, decreases liability
    CREDIT = "CREDIT"  # Decreases asset accounts, increases liability


@dataclass(frozen=True)
class LedgerEntry:
    """An immutable double-entry ledger line."""
    entry_id:      str
    journal_id:    str          # Groups a balanced set of entries
    account_id:    str          # Account being affected
    account_type:  AccountType
    entry_type:    EntryType
    amount_cents:  int          # Always positive
    description:   str
    created_at:    float        # Unix timestamp
    prev_hash:     str          # Hash of previous entry on this account
    entry_hash:    str          # SHA-256 of this entry's content


@dataclass
class Journal:
    """A balanced set of double-entry ledger entries."""
    journal_id:  str
    entries:     List[LedgerEntry]
    description: str
    created_at:  float

    def is_balanced(self) -> bool:
        debits  = sum(e.amount_cents for e in self.entries if e.entry_type == EntryType.DEBIT)
        credits = sum(e.amount_cents for e in self.entries if e.entry_type == EntryType.CREDIT)
        return debits == credits

    def assert_balanced(self) -> None:
        if not self.is_balanced():
            debits  = sum(e.amount_cents for e in self.entries if e.entry_type == EntryType.DEBIT)
            credits = sum(e.amount_cents for e in self.entries if e.entry_type == EntryType.CREDIT)
            raise LedgerImbalanceError(
                f"Journal {self.journal_id} is NOT balanced: "
                f"debits={debits} credits={credits} diff={debits - credits}"
            )


class LedgerImbalanceError(Exception):
    """Raised when a journal's debits do not equal credits."""
    pass


class TamperDetectedError(Exception):
    """Raised when a ledger entry hash chain is broken."""
    pass


class LedgerEngine:
    """
    Double-Entry Accounting Ledger Engine.

    Guarantees:
    * Every journal is balanced (debits == credits).
    * Entries are immutable after creation.
    * Hash chain links every entry to its predecessor for tamper detection.
    * All amounts are in integer cents — no floating-point arithmetic.
    """

    def __init__(self):
        # In-memory ledger (in production this is persisted to PostgreSQL)
        self._entries: List[LedgerEntry] = []
        self._account_heads: Dict[str, str] = {}   # account_id -> latest entry hash
        self._journals: Dict[str, Journal] = {}

    def _make_hash(self, entry_id: str, journal_id: str, account_id: str,
                   entry_type: str, amount_cents: int, created_at: float,
                   prev_hash: str) -> str:
        payload = "|".join([
            entry_id, journal_id, account_id,
            entry_type, str(amount_cents),
            str(created_at), prev_hash,
        ])
        return hashlib.sha256(payload.encode()).hexdigest()

    def post_journal(
        self,
        entries_spec: List[Dict],
        description: str,
    ) -> Journal:
        """
        Create and post a balanced journal.

        Args:
            entries_spec: List of dicts with keys:
                account_id    str
                account_type  AccountType
                entry_type    EntryType
                amount_cents  int  (>= 1)
                description   str
            description: Human-readable description of the journal.

        Returns:
            Committed Journal object.

        Raises:
            LedgerImbalanceError: if entries do not balance.
            ValueError:           if any amount is < 1.
        """
        for spec in entries_spec:
            if spec["amount_cents"] < 1:
                raise ValueError(
                    f"Ledger entry amount must be >= 1 cent, got {spec['amount_cents']}"
                )

        journal_id = str(uuid.uuid4())
        now = time.time()
        entries: List[LedgerEntry] = []

        for spec in entries_spec:
            acc_id  = spec["account_id"]
            prev_h  = self._account_heads.get(acc_id, "0" * 64)
            entry_id = str(uuid.uuid4())

            e_hash = self._make_hash(
                entry_id, journal_id, acc_id,
                spec["entry_type"], spec["amount_cents"],
                now, prev_h,
            )

            entry = LedgerEntry(
                entry_id     = entry_id,
                journal_id   = journal_id,
                account_id   = acc_id,
                account_type = spec["account_type"],
                entry_type   = spec["entry_type"],
                amount_cents = spec["amount_cents"],
                description  = spec.get("description", description),
                created_at   = now,
                prev_hash    = prev_h,
                entry_hash   = e_hash,
            )
            entries.append(entry)
            self._account_heads[acc_id] = e_hash

        journal = Journal(
            journal_id  = journal_id,
            entries     = entries,
            description = description,
            created_at  = now,
        )
        journal.assert_balanced()

        self._entries.extend(entries)
        self._journals[journal_id] = journal
        return journal

    def post_expense_journal(
        self,
        payer_id: str,
        splits: List[Dict],   # [{user_id, amount_cents}]
        description: str,
    ) -> Journal:
        """
        Post a double-entry journal for an expense:

        For each debtor (non-payer split):
            DR  debtor:USER_WALLET          amount
            CR  EXPENSE_CLEARING:{journal}  amount

        For the clearing account to payer:
            DR  EXPENSE_CLEARING:{journal}  total
            CR  payer:USER_WALLET           total

        Net effect: payer's wallet increases, debtors' wallets decrease.
        """
        clearing_id = f"EXPENSE_CLEARING:{uuid.uuid4()}"
        specs = []

        total = 0
        for split in splits:
            uid    = split["user_id"]
            amount = split["amount_cents"]
            if uid == payer_id:
                continue  # Payer's own share — no inter-user debt
            total += amount
            specs.append({"account_id": f"USER_WALLET:{uid}",     "account_type": AccountType.USER_WALLET,      "entry_type": EntryType.DEBIT,  "amount_cents": amount, "description": f"Owes {description}"})
            specs.append({"account_id": clearing_id,               "account_type": AccountType.EXPENSE_CLEARING, "entry_type": EntryType.CREDIT, "amount_cents": amount, "description": f"Expense clearing: {description}"})

        if total > 0:
            specs.append({"account_id": clearing_id,               "account_type": AccountType.EXPENSE_CLEARING, "entry_type": EntryType.DEBIT,  "amount_cents": total, "description": f"Close clearing: {description}"})
            specs.append({"account_id": f"USER_WALLET:{payer_id}", "account_type": AccountType.USER_WALLET,      "entry_type": EntryType.CREDIT, "amount_cents": total, "description": f"Paid for: {description}"})

        return self.post_journal(specs, description)

    def post_settlement_journal(
        self,
        payer_id: str,
        payee_id: str,
        amount_cents: int,
        description: str = "Settlement",
    ) -> Journal:
        """
        Post a double-entry journal for a settlement payment:
            DR  payer:USER_WALLET           amount
            CR  SETTLEMENT_CLEARING         amount
            DR  SETTLEMENT_CLEARING         amount
            CR  payee:USER_WALLET           amount
        """
        clearing_id = f"SETTLEMENT_CLEARING:{uuid.uuid4()}"
        specs = [
            {"account_id": f"USER_WALLET:{payer_id}", "account_type": AccountType.USER_WALLET,         "entry_type": EntryType.DEBIT,  "amount_cents": amount_cents, "description": f"Settlement payment to {payee_id}"},
            {"account_id": clearing_id,               "account_type": AccountType.SETTLEMENT_CLEARING, "entry_type": EntryType.CREDIT, "amount_cents": amount_cents, "description": "Settlement clearing"},
            {"account_id": clearing_id,               "account_type": AccountType.SETTLEMENT_CLEARING, "entry_type": EntryType.DEBIT,  "amount_cents": amount_cents, "description": "Settlement clearing close"},
            {"account_id": f"USER_WALLET:{payee_id}", "account_type": AccountType.USER_WALLET,         "entry_type": EntryType.CREDIT, "amount_cents": amount_cents, "description": f"Settlement received from {payer_id}"},
        ]
        return self.post_journal(specs, description)

    def verify_chain_integrity(self) -> Tuple[bool, List[str]]:
        """
        Verify the hash chain integrity of all ledger entries.
        Returns (ok: bool, errors: List[str]).
        """
        errors = []
        by_account: Dict[str, List[LedgerEntry]] = {}
        for entry in self._entries:
            by_account.setdefault(entry.account_id, []).append(entry)

        for acc_id, entries in by_account.items():
            entries.sort(key=lambda e: e.created_at)
            prev_hash = "0" * 64
            for entry in entries:
                if entry.prev_hash != prev_hash:
                    errors.append(
                        f"Chain broken at entry {entry.entry_id} "
                        f"on account {acc_id}: "
                        f"expected prev_hash={prev_hash!r}, "
                        f"got {entry.prev_hash!r}"
                    )
                expected = self._make_hash(
                    entry.entry_id, entry.journal_id, entry.account_id,
                    entry.entry_type, entry.amount_cents, entry.created_at,
                    entry.prev_hash,
                )
                if expected != entry.entry_hash:
                    errors.append(
                        f"Hash mismatch at entry {entry.entry_id}: tamper detected!"
                    )
                prev_hash = entry.entry_hash

        return (len(errors) == 0), errors

    def get_account_balance(self, account_id: str) -> int:
        """Returns net balance of an account in cents."""
        balance = 0
        for e in self._entries:
            if e.account_id != account_id:
                continue
            if e.entry_type == EntryType.CREDIT:
                balance += e.amount_cents
            else:
                balance -= e.amount_cents
        return balance

    def get_all_journals(self) -> List[Journal]:
        return list(self._journals.values())

    def audit_report(self) -> Dict:
        """Generate a full audit report."""
        total_debits  = sum(e.amount_cents for e in self._entries if e.entry_type == EntryType.DEBIT)
        total_credits = sum(e.amount_cents for e in self._entries if e.entry_type == EntryType.CREDIT)
        ok, errs = self.verify_chain_integrity()
        return {
            "total_entries":       len(self._entries),
            "total_journals":      len(self._journals),
            "total_debits_cents":  total_debits,
            "total_credits_cents": total_credits,
            "is_balanced":         total_debits == total_credits,
            "chain_integrity_ok":  ok,
            "chain_errors":        errs,
        }


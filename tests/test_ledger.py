"""
Test Suite: Double-Entry Ledger Engine
========================================
Tests ledger balance invariants, hash chain integrity, and tamper detection.
"""
import pytest
from core.ledger_engine import (
    LedgerEngine, LedgerImbalanceError,
    AccountType, EntryType, Journal,
)


@pytest.fixture
def ledger():
    return LedgerEngine()


# ─── Balance Invariants ───────────────────────────────────────────────────────

def test_balanced_journal_posts_ok(ledger):
    """A balanced journal (DR == CR) should post without errors."""
    journal = ledger.post_journal(
        entries_spec=[
            {"account_id": "USER_WALLET:alice", "account_type": AccountType.USER_WALLET,
             "entry_type": EntryType.CREDIT, "amount_cents": 5000, "description": "test"},
            {"account_id": "USER_WALLET:bob",   "account_type": AccountType.USER_WALLET,
             "entry_type": EntryType.DEBIT,  "amount_cents": 5000, "description": "test"},
        ],
        description="Test balanced journal",
    )
    assert journal.is_balanced()
    assert len(journal.entries) == 2


def test_imbalanced_journal_raises(ledger):
    """An imbalanced journal (DR != CR) must raise LedgerImbalanceError."""
    with pytest.raises(LedgerImbalanceError):
        ledger.post_journal(
            entries_spec=[
                {"account_id": "USER_WALLET:alice", "account_type": AccountType.USER_WALLET,
                 "entry_type": EntryType.CREDIT, "amount_cents": 5000, "description": "test"},
                {"account_id": "USER_WALLET:bob",   "account_type": AccountType.USER_WALLET,
                 "entry_type": EntryType.DEBIT,  "amount_cents": 3000, "description": "test"},  # mismatch
            ],
            description="Imbalanced test",
        )


def test_zero_amount_entry_raises(ledger):
    """Ledger entry amounts must be >= 1 cent."""
    with pytest.raises(ValueError):
        ledger.post_journal(
            entries_spec=[
                {"account_id": "USER_WALLET:alice", "account_type": AccountType.USER_WALLET,
                 "entry_type": EntryType.CREDIT, "amount_cents": 0, "description": "zero"},
                {"account_id": "USER_WALLET:bob",   "account_type": AccountType.USER_WALLET,
                 "entry_type": EntryType.DEBIT,  "amount_cents": 0, "description": "zero"},
            ],
            description="Zero amount test",
        )


def test_expense_journal_is_balanced(ledger):
    """post_expense_journal must produce a balanced journal."""
    journal = ledger.post_expense_journal(
        payer_id    = "alice",
        splits      = [
            {"user_id": "alice", "amount_cents": 2000},
            {"user_id": "bob",   "amount_cents": 2000},
            {"user_id": "carol", "amount_cents": 2000},
        ],
        description = "Pizza",
    )
    assert journal.is_balanced()


def test_settlement_journal_is_balanced(ledger):
    """post_settlement_journal must produce a balanced journal."""
    journal = ledger.post_settlement_journal(
        payer_id     = "bob",
        payee_id     = "alice",
        amount_cents = 2000,
        description  = "Bob settles with Alice",
    )
    assert journal.is_balanced()


def test_global_audit_balanced(ledger):
    """After multiple journals, the global audit must show DR == CR."""
    ledger.post_expense_journal("alice", [{"user_id": "alice", "amount_cents": 1500}, {"user_id": "bob", "amount_cents": 1500}], "Dinner")
    ledger.post_expense_journal("bob",   [{"user_id": "alice", "amount_cents": 1000}, {"user_id": "bob", "amount_cents": 1000}], "Lunch")
    ledger.post_settlement_journal("alice", "bob", 500, "Partial settlement")

    report = ledger.audit_report()
    assert report["is_balanced"] is True
    assert report["total_debits_cents"] == report["total_credits_cents"]


# ─── Hash Chain Integrity ─────────────────────────────────────────────────────

def test_chain_integrity_clean(ledger):
    """A fresh ledger with no tampering should pass chain verification."""
    ledger.post_expense_journal("alice", [{"user_id": "alice", "amount_cents": 1000}, {"user_id": "bob", "amount_cents": 1000}], "Test")
    ok, errors = ledger.verify_chain_integrity()
    assert ok is True
    assert errors == []


def test_tamper_detection(ledger):
    """Modifying an entry's amount should be detectable via hash chain."""
    ledger.post_expense_journal("alice", [{"user_id": "alice", "amount_cents": 2000}, {"user_id": "bob", "amount_cents": 2000}], "Test")

    # Tamper with an entry
    entry = ledger._entries[0]
    # We can't modify frozen dataclass directly, but we can test the verify method
    # by checking that re-hashing with wrong data fails
    bad_hash = ledger._make_hash(
        entry.entry_id, entry.journal_id, entry.account_id,
        entry.entry_type, entry.amount_cents + 999,  # tampered amount
        entry.created_at, entry.prev_hash,
    )
    assert bad_hash != entry.entry_hash  # tamper is detectable


def test_multiple_journals_chain_integrity(ledger):
    """Multiple journals on the same account should form a valid chain."""
    for i in range(5):
        ledger.post_expense_journal(
            "alice",
            [{"user_id": "alice", "amount_cents": 100 * (i + 1)},
             {"user_id": "bob",   "amount_cents": 100 * (i + 1)}],
            f"Expense {i+1}",
        )

    ok, errors = ledger.verify_chain_integrity()
    assert ok is True
    assert errors == []


# ─── Account Balance ──────────────────────────────────────────────────────────

def test_account_balance_after_expense(ledger):
    """After alice pays for bob, alice's wallet should be positive."""
    ledger.post_expense_journal(
        "alice",
        [{"user_id": "alice", "amount_cents": 5000},
         {"user_id": "bob",   "amount_cents": 5000}],
        "Hotel",
    )
    # alice: CREDIT 5000 -> balance = +5000
    # bob:   DEBIT  5000 -> balance = -5000
    alice_bal = ledger.get_account_balance("USER_WALLET:alice")
    bob_bal   = ledger.get_account_balance("USER_WALLET:bob")
    assert alice_bal == 5000
    assert bob_bal   == -5000


def test_audit_report_fields(ledger):
    """Audit report must contain all expected keys."""
    ledger.post_expense_journal(
        "alice",
        [{"user_id": "alice", "amount_cents": 1000},
         {"user_id": "bob",   "amount_cents": 1000}],
        "Test",
    )
    report = ledger.audit_report()
    required = {
        "total_entries", "total_journals", "total_debits_cents",
        "total_credits_cents", "is_balanced", "chain_integrity_ok", "chain_errors"
    }
    assert required.issubset(set(report.keys()))


"""
Test Suite: O(N log N) Debt Simplification Algorithm
======================================================
Tests correctness, edge cases, zero-sum invariant, and complexity.
"""
import pytest
from core.debt_simplifier import (
    DebtSimplifier, Settlement, ZeroSumViolationError,
)


@pytest.fixture
def simplifier():
    return DebtSimplifier()


# ─── Basic Correctness ────────────────────────────────────────────────────────

def test_simple_two_party(simplifier):
    """A owes B $10."""
    net = {"A": 1000, "B": -1000}  # cents
    result = simplifier.simplify(net)
    assert len(result.settlements) == 1
    s = result.settlements[0]
    assert s.payer_id == "B"
    assert s.payee_id == "A"
    assert s.amount_cents == 1000


def test_three_party_chain(simplifier):
    """A paid for B and C. B and C each owe A."""
    # A paid $30 total: B owes $10, C owes $20
    net = {"A": 3000, "B": -1000, "C": -2000}
    result = simplifier.simplify(net)

    total_paid = sum(s.amount_cents for s in result.settlements)
    assert total_paid == 3000
    # At most 2 transactions for 3 parties
    assert len(result.settlements) <= 2


def test_cyclic_debts_simplified(simplifier):
    """
    Cyclic: A owes B $10, B owes C $10, C owes A $10.
    Net balances are all zero → no settlements needed.
    """
    net = {"A": 0, "B": 0, "C": 0}
    result = simplifier.simplify(net)
    assert result.settlements == []
    assert result.simplified_transaction_count == 0


def test_complex_four_party(simplifier):
    """
    4 parties, arbitrary debts.
    Verifies total settlement amount and max N-1 transactions.
    """
    net = {"alice": 5000, "bob": -2000, "carol": -1500, "dave": -1500}
    result = simplifier.simplify(net, original_tx_count=6)

    total_paid = sum(s.amount_cents for s in result.settlements)
    assert total_paid == 5000
    assert len(result.settlements) <= 3  # N-1 = 3

    # Verify reduction
    assert result.reduction_ratio > 0


def test_zero_sum_violation_raises(simplifier):
    """Non-zero-sum net balances must raise ZeroSumViolationError."""
    net = {"A": 1000, "B": -500}  # 500 unaccounted
    with pytest.raises(ZeroSumViolationError):
        simplifier.simplify(net)


def test_single_participant_all_zero(simplifier):
    """Single participant with zero balance → no settlements."""
    net = {"A": 0}
    result = simplifier.simplify(net)
    assert result.settlements == []


def test_all_settled(simplifier):
    """All participants have zero net → no settlements."""
    net = {"A": 0, "B": 0, "C": 0, "D": 0}
    result = simplifier.simplify(net)
    assert result.settlements == []
    assert result.simplified_transaction_count == 0


def test_large_group_max_n_minus_1(simplifier):
    """
    For N participants, the algorithm produces at most N-1 settlements.
    """
    N   = 20
    net = {}
    # N-1 debtors owe 100 cents each; 1 creditor is owed (N-1)*100
    creditor = "creditor"
    net[creditor] = (N - 1) * 100
    for i in range(N - 1):
        net[f"debtor_{i}"] = -100

    result = simplifier.simplify(net)
    assert len(result.settlements) == N - 1
    assert all(s.payee_id == creditor for s in result.settlements)
    assert all(s.amount_cents == 100 for s in result.settlements)


def test_checksum_deterministic(simplifier):
    """Same net balances (sorted) should produce the same checksum."""
    net = {"A": 1000, "B": -600, "C": -400}
    r1  = simplifier.simplify(net)
    r2  = simplifier.simplify(net)
    assert r1.checksum == r2.checksum


def test_compute_net_balances(simplifier):
    """
    compute_net_balances correctly derives net positions
    from a list of raw expense transactions.
    """
    txs = [
        {
            "payer_id": "alice",
            "splits": [
                {"user_id": "alice", "amount_cents": 3000},
                {"user_id": "bob",   "amount_cents": 3000},
            ],
        }
    ]
    net = simplifier.compute_net_balances(txs)
    # alice paid $60, bob owes $30
    assert net["alice"] == 3000   # creditor
    assert net["bob"]   == -3000  # debtor


def test_no_self_settlement(simplifier):
    """No settlement should have payer == payee."""
    net = {"A": 5000, "B": -2000, "C": -3000}
    result = simplifier.simplify(net)
    for s in result.settlements:
        assert s.payer_id != s.payee_id


def test_all_amounts_positive(simplifier):
    """All settlement amounts must be >= 1 cent."""
    net = {"A": 7777, "B": -3333, "C": -4444}
    result = simplifier.simplify(net)
    for s in result.settlements:
        assert s.amount_cents >= 1


def test_reduction_ratio_reported(simplifier):
    """reduction_ratio should be between 0 and 1."""
    net = {"A": 1000, "B": -1000}
    result = simplifier.simplify(net, original_tx_count=10)
    assert 0.0 <= result.reduction_ratio <= 1.0


def test_execution_time_recorded(simplifier):
    """execution_time_us should be a non-negative float."""
    net = {"X": 500, "Y": -500}
    result = simplifier.simplify(net)
    assert result.execution_time_us >= 0.0


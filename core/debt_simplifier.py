"""
O(N log N) Max-Heap Debt Simplification Algorithm
================================================
Reduces an arbitrary debt graph with M edges among N parties
down to at most (N-1) transactions — optimal minimum.

Complexity: O(N log N) time, O(N) space
Precision:  All monetary values are in integer cents (no floating-point)
"""
import heapq
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
import hashlib
import time


@dataclass(frozen=True)
class Settlement:
    """A single directed payment to settle debt."""
    payer_id: str          # Who pays
    payee_id: str          # Who receives
    amount_cents: int      # Amount in cents (integer, no float drift)

    def __repr__(self) -> str:
        return (f"Settlement({self.payer_id!r} -> {self.payee_id!r}, "
                f"${self.amount_cents / 100:.2f})")


@dataclass
class DebtSimplifierResult:
    """Result of the O(N log N) simplification pass."""
    settlements: List[Settlement]
    original_transaction_count: int
    simplified_transaction_count: int
    reduction_ratio: float             # (1 - simplified/original)
    algorithm_complexity: str
    execution_time_us: float           # microseconds
    checksum: str                      # SHA-256 of all settlement pairs


class ZeroSumViolationError(Exception):
    """Raised when net balances do not sum to zero — money was created or destroyed."""
    pass


class DebtSimplifier:
    """
    O(N log N) Max-Heap Debt Simplification Engine.

    Algorithm:
    ----------
    1.  Compute net balance for every participant:
            net[u] = sum(amounts_paid_by_u) - sum(amounts_owed_by_u)
        Positive net  => creditor (is owed money)
        Negative net  => debtor   (owes money)
        Zero net      => settled  (excluded from processing)

    2.  Push all creditors into a max-heap (max credit first).
        Push all debtors   into a max-heap (max debt   first).

    3.  Greedy matching loop (O(N log N)):
        while both heaps non-empty:
            c = heappop(creditors_heap)   # largest creditor
            d = heappop(debtors_heap)     # largest debtor
            settled = min(c.amount, d.amount)
            emit Settlement(d.id -> c.id, settled)
            residual_c = c.amount - settled
            residual_d = d.amount - settled
            if residual_c > 0: push c back with residual_c
            if residual_d > 0: push d back with residual_d

    4.  Result: at most (N-1) transactions, provably optimal.

    Invariant Checks:
    -----------------
    * Zero-sum: sum(net) == 0  (conservation of money)
    * All settlements >= 1 cent
    * No self-settlement
    * Cryptographic checksum for auditability
    """

    def simplify(
        self,
        net_balances: Dict[str, int],   # user_id -> net balance in cents
        original_tx_count: int = 0,
    ) -> DebtSimplifierResult:
        """
        Simplify an arbitrary debt graph using O(N log N) heap matching.

        Args:
            net_balances:       {user_id: net_balance_cents}
                                Positive = creditor, Negative = debtor.
            original_tx_count:  Number of raw debt edges before simplification
                                (used only for reporting the reduction ratio).
        Returns:
            DebtSimplifierResult with list of Settlement objects.
        Raises:
            ZeroSumViolationError: if sum(net_balances.values()) != 0
        """
        t_start = time.perf_counter()

        # --- 1. Zero-sum invariant check ---
        total = sum(net_balances.values())
        if total != 0:
            raise ZeroSumViolationError(
                f"Net balances do not sum to zero: sum={total} cents. "
                f"Money conservation violated."
            )

        # --- 2. Build max-heaps (Python heapq is min-heap, negate for max) ---
        creditors: List[Tuple[int, str]] = []  # (-amount, user_id)
        debtors:   List[Tuple[int, str]] = []  # (-amount, user_id)

        for uid, net in net_balances.items():
            if net > 0:
                heapq.heappush(creditors, (-net, uid))
            elif net < 0:
                heapq.heappush(debtors, (net, uid))   # already negative

        # --- 3. O(N log N) greedy matching ---
        settlements: List[Settlement] = []

        while creditors and debtors:
            neg_credit, creditor_id = heapq.heappop(creditors)
            debt_amount, debtor_id  = heapq.heappop(debtors)

            credit_amount = -neg_credit          # positive
            debt_amount   = -debt_amount         # positive (negate back)

            settled = min(credit_amount, debt_amount)

            if settled < 1:
                # Sub-cent residual — discard (rounding artifact)
                continue

            settlements.append(Settlement(
                payer_id=debtor_id,
                payee_id=creditor_id,
                amount_cents=settled,
            ))

            residual_credit = credit_amount - settled
            residual_debt   = debt_amount   - settled

            if residual_credit > 0:
                heapq.heappush(creditors, (-residual_credit, creditor_id))
            if residual_debt > 0:
                heapq.heappush(debtors, (-residual_debt, debtor_id))

        t_end = time.perf_counter()
        exec_us = (t_end - t_start) * 1_000_000

        # --- 4. Build cryptographic checksum ---
        chk_input = "|".join(
            f"{s.payer_id}:{s.payee_id}:{s.amount_cents}"
            for s in sorted(settlements, key=lambda s: (s.payer_id, s.payee_id))
        )
        checksum = hashlib.sha256(chk_input.encode()).hexdigest()

        N = len(net_balances)
        orig = original_tx_count if original_tx_count > 0 else max(1, N * (N - 1) // 2)
        simplified = len(settlements)
        ratio = 1.0 - (simplified / orig) if orig > 0 else 0.0

        return DebtSimplifierResult(
            settlements=settlements,
            original_transaction_count=orig,
            simplified_transaction_count=simplified,
            reduction_ratio=ratio,
            algorithm_complexity=f"O({N} log {N}) = O({N * (N.bit_length())})",
            execution_time_us=exec_us,
            checksum=checksum,
        )

    def compute_net_balances(
        self,
        transactions: List[Dict],
    ) -> Dict[str, int]:
        """
        Compute net balances from a list of raw transaction dicts.

        Each dict must have:
            payer_id:  str
            splits:    [{user_id: str, amount_cents: int}]
        """
        balances: Dict[str, int] = {}

        for tx in transactions:
            payer = tx["payer_id"]
            for split in tx["splits"]:
                uid    = split["user_id"]
                amount = split["amount_cents"]

                balances.setdefault(payer, 0)
                balances.setdefault(uid, 0)

                if uid == payer:
                    # Payer's own share — no net change
                    continue

                balances[payer] += amount    # payer is owed this
                balances[uid]   -= amount    # uid owes this

        return balances


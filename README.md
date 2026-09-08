# Distributed Expense Settlement Engine

A high-performance, fault-tolerant backend system for multi-party expense sharing and debt settlement.

## Core Architecture

| Component | Technology | Details |
|---|---|---|
| **Debt Simplification** | O(N log N) Max-Heap | Reduces N²→(N-1) transactions |
| **Accounting Ledger** | Double-Entry + SHA-256 chains | Every journal: Σ Debits = Σ Credits |
| **Concurrency Control** | OCC + Exponential Backoff | Version-checked commits, no locks |
| **Message Bus** | Redis Streams (fallback: asyncio.Queue) | Transactional Outbox Pattern |
| **Cache** | Redis (fallback: InMemoryCache) | Idempotency keys + Balance cache |
| **API** | FastAPI (async) | Auto-generated OpenAPI docs |
| **Database** | PostgreSQL (fallback: SQLite) | Versioned entities, append-only ledger |

---

## Quick Start (Local, No Docker Required)

```bash
# 1. Create virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# or: source .venv/bin/activate  # Linux/Mac

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the API server (auto-selects SQLite + InMemoryCache)
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# 4. Open the interactive API docs
# http://localhost:8000/docs
```

---

## Project Structure

```
expense_settlement_engine/
├── core/
│   ├── debt_simplifier.py    ← O(N log N) Max-Heap algorithm
│   ├── ledger_engine.py      ← Double-entry accounting + hash chains
│   └── occ.py                ← Optimistic Concurrency Control + retry
├── database/
│   ├── models.py             ← SQLAlchemy ORM with version columns
│   ├── connection.py         ← Async engine (PostgreSQL / SQLite)
│   └── store.py              ← In-memory data store (fallback)
├── messaging/
│   ├── events.py             ← Event schema dataclasses
│   ├── outbox.py             ← Transactional Outbox
│   └── queue.py              ← Redis Streams + InMemoryQueue fallback
├── cache/
│   ├── redis_client.py       ← Redis + InMemoryCache fallback
│   └── idempotency.py        ← Idempotency key management
├── services/
│   ├── expense_service.py    ← Expense creation + split logic (OCC)
│   └── group_service.py      ← User and group management
├── worker/
│   └── worker.py             ← Async settlement worker daemon
├── api/
│   ├── main.py               ← FastAPI app + lifespan
│   ├── routes/
│   │   ├── users.py
│   │   ├── groups.py         ← Balances, simplify, settle endpoints
│   │   ├── expenses.py       ← Idempotent expense creation
│   │   └── ledger.py         ← Audit endpoint
│   └── schemas/
│       └── schemas.py        ← Pydantic request/response models
├── tests/
│   ├── test_debt_simplifier.py
│   ├── test_ledger.py
│   ├── test_occ.py
│   ├── test_queue.py
│   └── test_api.py
├── scripts/
│   ├── benchmark_heap.py         ← O(N log N) scaling proof
│   └── benchmark_concurrency.py  ← OCC stress test
├── requirements.txt
├── pytest.ini
├── Dockerfile
└── docker-compose.yml
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/users` | Create a user |
| `GET`  | `/api/v1/users` | List all users |
| `POST` | `/api/v1/groups` | Create a group |
| `POST` | `/api/v1/groups/{id}/members` | Add member to group |
| `POST` | `/api/v1/groups/{id}/expenses` | Create expense (idempotent, OCC) |
| `GET`  | `/api/v1/groups/{id}/expenses` | List group expenses |
| `GET`  | `/api/v1/groups/{id}/balances` | Get net balances (cached) |
| `GET`  | `/api/v1/groups/{id}/simplify` | Preview O(N log N) debt graph |
| `POST` | `/api/v1/groups/{id}/settle` | Trigger async settlement |
| `GET`  | `/api/v1/ledger/audit` | Ledger audit (DR=CR, hash chain) |
| `GET`  | `/api/v1/health` | System health check |

---

## Run Tests

```bash
# All tests
pytest -v

# Individual suites
pytest tests/test_debt_simplifier.py -v    # Algorithm
pytest tests/test_ledger.py -v             # Ledger invariants
pytest tests/test_occ.py -v               # OCC conflict/retry
pytest tests/test_queue.py -v             # Message queue/outbox
pytest tests/test_api.py -v               # End-to-end API

# Coverage report
pytest --tb=short -q
```

---

## Benchmarks

```bash
# O(N log N) scaling proof (N = 10 to 50,000)
python scripts/benchmark_heap.py

# OCC under concurrent load
python scripts/benchmark_concurrency.py
```

---

## The Algorithm: O(N log N) Max-Heap Debt Simplification

```
Input:  net[u] = Σ paid_by_u - Σ owed_by_u   (for all N participants)
Output: at most (N-1) settlement transactions

Steps:
  1. Verify Σ net[u] = 0  [conservation invariant]
  2. Build max-heap of creditors (owed money)
  3. Build max-heap of debtors   (owe money)
  4. WHILE both heaps non-empty:
       c ← pop max creditor
       d ← pop max debtor
       settled ← min(c.amount, d.amount)
       emit Payment(from=d, to=c, amount=settled)
       push residuals back if non-zero
  5. Return: at most N-1 payments (provably optimal)

Complexity: O(N log N) — each pop/push is O(log N), at most 2N operations
```

---

## Double-Entry Ledger Guarantee

Every expense produces balanced journal entries:

```
Expense: Alice pays $90 for 3 people (each owes $30)

DR  USER_WALLET:bob       $30   ← Bob owes
CR  EXPENSE_CLEARING      $30
DR  USER_WALLET:carol     $30   ← Carol owes
CR  EXPENSE_CLEARING      $30
DR  EXPENSE_CLEARING      $60   ← Close clearing
CR  USER_WALLET:alice     $60   ← Alice is owed

Σ Debits = $90 = Σ Credits  ✓
```

Hash chain: each entry records `SHA-256(prev_entry_hash || content)` for tamper detection.

---

## OCC: Optimistic Concurrency Control

```sql
-- Version-checked update (simulated in Python store)
UPDATE accounts
SET    balance_cents = balance_cents + :delta,
       version       = version + 1
WHERE  id            = :user_id
AND    version       = :expected_version;

-- If rowcount == 0 → concurrent writer won → raise OCC conflict → retry
```

Retry strategy: truncated exponential backoff with ±25% jitter, up to 5 retries.

---

## Production Deployment

```bash
# Start all services
docker compose up -d

# API available at http://localhost:8000
# Docs:   http://localhost:8000/docs
# Audit:  http://localhost:8000/api/v1/ledger/audit
# Health: http://localhost:8000/api/v1/health
```


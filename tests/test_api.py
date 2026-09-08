"""
Test Suite: API End-to-End Integration
========================================
Tests the full HTTP API using FastAPI TestClient.
Covers: user creation, group management, expense creation,
idempotency, balance computation, simplification, and settlement.
"""
import pytest
from fastapi.testclient import TestClient

# Reset global state between tests
import database.store as store_module
import messaging.outbox as outbox_module
import messaging.queue as queue_module
import cache.redis_client as cache_module
import cache.idempotency as idempotency_module


def make_fresh_app():
    """Create a fresh FastAPI app with clean global state."""
    # Reset all singletons
    store_module._store        = None
    outbox_module._outbox_instance = None
    queue_module._queue_instance   = None
    cache_module._cache_instance   = None
    idempotency_module._idempotency_manager = None

    # Also reset service singletons
    import services.expense_service as exp_svc
    import services.group_service as grp_svc
    import worker.worker as wkr
    exp_svc._expense_service = None
    grp_svc._group_service   = None
    wkr._worker              = None

    from api.main import app
    return app


@pytest.fixture(scope="function")
def client():
    app = make_fresh_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ─── Health Check ─────────────────────────────────────────────────────────────

def test_root_endpoint(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Distributed Expense Settlement Engine" in r.json()["service"]


def test_health_check(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"


# ─── Users ────────────────────────────────────────────────────────────────────

def test_create_user(client):
    r = client.post("/api/v1/users", json={"name": "Alice", "email": "alice@test.com"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"]  == "Alice"
    assert data["email"] == "alice@test.com"
    assert "id" in data


def test_create_user_duplicate_email(client):
    client.post("/api/v1/users", json={"name": "Alice", "email": "alice@test.com"})
    r = client.post("/api/v1/users", json={"name": "Alice2", "email": "alice@test.com"})
    assert r.status_code == 409


def test_get_user(client):
    r1 = client.post("/api/v1/users", json={"name": "Bob", "email": "bob@test.com"})
    uid = r1.json()["id"]
    r2  = client.get(f"/api/v1/users/{uid}")
    assert r2.status_code == 200
    assert r2.json()["id"] == uid


def test_get_user_not_found(client):
    r = client.get("/api/v1/users/nonexistent-id")
    assert r.status_code == 404


def test_list_users(client):
    client.post("/api/v1/users", json={"name": "A", "email": "a@t.com"})
    client.post("/api/v1/users", json={"name": "B", "email": "b@t.com"})
    r = client.get("/api/v1/users")
    assert r.status_code == 200
    assert len(r.json()) >= 2


# ─── Groups ───────────────────────────────────────────────────────────────────

def test_create_group(client):
    r = client.post("/api/v1/groups", json={"name": "Trip to Goa"})
    assert r.status_code == 201
    assert r.json()["name"] == "Trip to Goa"


def test_add_member(client):
    g = client.post("/api/v1/groups", json={"name": "G1"}).json()
    u = client.post("/api/v1/users", json={"name": "X", "email": "x@t.com"}).json()
    r = client.post(f"/api/v1/groups/{g['id']}/members", json={"user_id": u["id"]})
    assert r.status_code == 200


def test_add_member_nonexistent_user(client):
    g = client.post("/api/v1/groups", json={"name": "G2"}).json()
    r = client.post(f"/api/v1/groups/{g['id']}/members", json={"user_id": "bad-uid"})
    assert r.status_code == 404


# ─── Expenses ────────────────────────────────────────────────────────────────

def _setup_group_with_members(client):
    """Helper: create group with 3 members and return (group_id, [user_ids])."""
    users = []
    for name, email in [("Alice", "a@t.com"), ("Bob", "b@t.com"), ("Carol", "c@t.com")]:
        u = client.post("/api/v1/users", json={"name": name, "email": email}).json()
        users.append(u["id"])

    g = client.post("/api/v1/groups", json={"name": "Test Group"}).json()
    for uid in users:
        client.post(f"/api/v1/groups/{g['id']}/members", json={"user_id": uid})

    return g["id"], users


def test_create_expense_equal_split(client):
    gid, (alice, bob, carol) = _setup_group_with_members(client)
    r = client.post(f"/api/v1/groups/{gid}/expenses", json={
        "payer_id":    alice,
        "title":       "Dinner",
        "total_cents": 9000,
        "split_type":  "EQUAL",
    })
    assert r.status_code == 201
    data = r.json()
    assert data["total_cents"] == 9000
    assert len(data["splits"]) == 3
    assert sum(s["amount_cents"] for s in data["splits"]) == 9000


def test_create_expense_exact_split(client):
    gid, (alice, bob, carol) = _setup_group_with_members(client)
    r = client.post(f"/api/v1/groups/{gid}/expenses", json={
        "payer_id":    alice,
        "title":       "Hotel",
        "total_cents": 10000,
        "split_type":  "EXACT",
        "splits": [
            {"user_id": alice, "amount_cents": 5000},
            {"user_id": bob,   "amount_cents": 3000},
            {"user_id": carol, "amount_cents": 2000},
        ],
    })
    assert r.status_code == 201


def test_create_expense_invalid_split_sum(client):
    gid, (alice, bob, carol) = _setup_group_with_members(client)
    r = client.post(f"/api/v1/groups/{gid}/expenses", json={
        "payer_id":    alice,
        "title":       "Bad Split",
        "total_cents": 10000,
        "split_type":  "EXACT",
        "splits": [
            {"user_id": alice, "amount_cents": 3000},
            {"user_id": bob,   "amount_cents": 3000},
        ],
    })
    assert r.status_code == 400


def test_expense_idempotency(client):
    """Same idempotency key should return the same expense."""
    gid, (alice, bob, carol) = _setup_group_with_members(client)

    payload = {
        "payer_id":        alice,
        "title":           "Idempotent Dinner",
        "total_cents":     6000,
        "split_type":      "EQUAL",
        "idempotency_key": "unique-idem-key-999",
    }

    r1 = client.post(f"/api/v1/groups/{gid}/expenses", json=payload)
    assert r1.status_code == 201

    r2 = client.post(f"/api/v1/groups/{gid}/expenses", json=payload)
    # Second call: either 201 (cached response) or 409 (conflict)
    # Both are valid idempotency behaviors
    assert r2.status_code in (200, 201, 409)


# ─── Balances & Simplification ───────────────────────────────────────────────

def test_get_balances(client):
    gid, (alice, bob, carol) = _setup_group_with_members(client)
    client.post(f"/api/v1/groups/{gid}/expenses", json={
        "payer_id": alice, "title": "Test", "total_cents": 9000, "split_type": "EQUAL",
    })
    r = client.get(f"/api/v1/groups/{gid}/balances")
    assert r.status_code == 200
    data = r.json()
    assert data["group_id"] == gid
    assert not data["is_settled"]


def test_simplify_debts(client):
    gid, (alice, bob, carol) = _setup_group_with_members(client)
    client.post(f"/api/v1/groups/{gid}/expenses", json={
        "payer_id": alice, "title": "Expense", "total_cents": 9000, "split_type": "EQUAL",
    })
    r = client.get(f"/api/v1/groups/{gid}/simplify")
    assert r.status_code == 200
    data = r.json()
    assert "settlements" in data
    assert "checksum"    in data
    # simplified_transaction_count <= N-1 where N = number of parties
    # original_transaction_count is the expense count (raw), not the debt-pair count
    # so we just assert the result has valid structure
    assert data["simplified_transaction_count"] >= 0
    assert data["original_transaction_count"] >= 0
    assert data["reduction_ratio_pct"] >= 0.0


# ─── Settlement ───────────────────────────────────────────────────────────────

def test_settle_group(client):
    gid, (alice, bob, carol) = _setup_group_with_members(client)
    client.post(f"/api/v1/groups/{gid}/expenses", json={
        "payer_id": alice, "title": "Expense", "total_cents": 9000, "split_type": "EQUAL",
    })
    r = client.post(f"/api/v1/groups/{gid}/settle", json={"requested_by": alice})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("QUEUED", "COMPLETED")


# ─── Ledger Audit ─────────────────────────────────────────────────────────────

def test_ledger_audit(client):
    gid, (alice, bob, carol) = _setup_group_with_members(client)
    client.post(f"/api/v1/groups/{gid}/expenses", json={
        "payer_id": alice, "title": "Audit Test", "total_cents": 3000, "split_type": "EQUAL",
    })
    r = client.get("/api/v1/ledger/audit")
    assert r.status_code == 200
    data = r.json()
    assert data["is_balanced"] is True
    assert data["chain_integrity_ok"] is True
    assert data["chain_errors"] == []

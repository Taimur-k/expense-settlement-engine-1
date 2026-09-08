import urllib.request, json, time

BASE = "http://localhost:8000/api/v1"


def post(path, data):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}") as r:
        return json.loads(r.read())


# ── Create Users ──────────────────────────────────────────────────────────────
print("=" * 55)
print("  DISTRIBUTED EXPENSE SETTLEMENT ENGINE — LIVE DEMO")
print("=" * 55)

print("\n[1] Creating Users...")
alice = post("/users", {"name": "Alice", "email": "alice@demo.com"})
bob   = post("/users", {"name": "Bob",   "email": "bob@demo.com"})
carol = post("/users", {"name": "Carol", "email": "carol@demo.com"})
dave  = post("/users", {"name": "Dave",  "email": "dave@demo.com"})
user_names = {alice["id"]: "Alice", bob["id"]: "Bob", carol["id"]: "Carol", dave["id"]: "Dave"}

for name, u in [("Alice", alice), ("Bob", bob), ("Carol", carol), ("Dave", dave)]:
    print(f"    {name:6}  id={u['id']}")

# ── Create Group ──────────────────────────────────────────────────────────────
print("\n[2] Creating Group: 'Weekend Trip to Goa'")
grp = post("/groups", {"name": "Weekend Trip to Goa"})
gid = grp["id"]
print(f"    Group id={gid}")

# ── Add Members ───────────────────────────────────────────────────────────────
print("\n[3] Adding Members...")
for u in [alice, bob, carol, dave]:
    post(f"/groups/{gid}/members", {"user_id": u["id"]})
    print(f"    Added {user_names[u['id']]}")

# ── Record Expenses ───────────────────────────────────────────────────────────
print("\n[4] Recording Expenses...")

e1 = post(f"/groups/{gid}/expenses", {
    "payer_id": alice["id"],
    "title": "Hotel (2 nights)",
    "total_cents": 24000,
    "split_type": "EQUAL",
    "idempotency_key": "exp-hotel-001",
})
print(f"    Alice  paid Rs.240  'Hotel'          EQUAL split  -> journal {e1['journal_id'][:8]}...")

e2 = post(f"/groups/{gid}/expenses", {
    "payer_id": bob["id"],
    "title": "Seafood Dinner",
    "total_cents": 8000,
    "split_type": "EXACT",
    "idempotency_key": "exp-dinner-001",
    "splits": [
        {"user_id": alice["id"], "amount_cents": 3000},
        {"user_id": bob["id"],   "amount_cents": 2000},
        {"user_id": carol["id"], "amount_cents": 2000},
        {"user_id": dave["id"],  "amount_cents": 1000},
    ],
})
print(f"    Bob    paid Rs.80   'Seafood Dinner' EXACT split  -> journal {e2['journal_id'][:8]}...")

e3 = post(f"/groups/{gid}/expenses", {
    "payer_id": carol["id"],
    "title": "Taxi & Transport",
    "total_cents": 6000,
    "split_type": "EQUAL",
    "idempotency_key": "exp-taxi-001",
})
print(f"    Carol  paid Rs.60   'Taxi'           EQUAL split  -> journal {e3['journal_id'][:8]}...")

e4 = post(f"/groups/{gid}/expenses", {
    "payer_id": dave["id"],
    "title": "Beach Bar Drinks",
    "total_cents": 4000,
    "split_type": "PERCENTAGE",
    "idempotency_key": "exp-drinks-001",
    "splits": [
        {"user_id": alice["id"], "percentage": 40},
        {"user_id": bob["id"],   "percentage": 30},
        {"user_id": carol["id"], "percentage": 20},
        {"user_id": dave["id"],  "percentage": 10},
    ],
})
print(f"    Dave   paid Rs.40   'Beach Drinks'   PERCENTAGE   -> journal {e4['journal_id'][:8]}...")

# ── Net Balances ──────────────────────────────────────────────────────────────
print("\n[5] Net Balances (creditor = owed, debtor = owes)...")
bal = get(f"/groups/{gid}/balances")
for b in sorted(bal["member_balances"], key=lambda x: -x["net_cents"]):
    name = user_names.get(b["user_id"], b["user_id"][:6])
    sign = "+" if b["net_cents"] > 0 else ""
    print(f"    {name:6}  {b['role']:<10}  {sign}Rs.{abs(b['net_dollars']):>6.2f}")

# ── O(N log N) Simplification ─────────────────────────────────────────────────
print("\n[6] O(N log N) Max-Heap Simplified Debts...")
simp = get(f"/groups/{gid}/simplify")
print(f"    Algorithm : {simp['algorithm']}")
print(f"    Exec time : {simp['execution_time_us']:.2f} us")
print(f"    Reduction : {simp['reduction_ratio_pct']}%  ({simp['simplified_transaction_count']} tx from {simp['original_transaction_count']})")
print(f"    Checksum  : {simp['checksum'][:20]}...")
print()
for i, s in enumerate(simp["settlements"], 1):
    payer = user_names.get(s["payer_id"], s["payer_id"][:6])
    payee = user_names.get(s["payee_id"], s["payee_id"][:6])
    print(f"    {i}. {payer:6} -> {payee:6}  pays Rs.{s['amount_dollars']:.2f}")

# ── Settlement ────────────────────────────────────────────────────────────────
print("\n[7] Triggering Settlement (async worker)...")
settle = post(f"/groups/{gid}/settle", {"requested_by": alice["id"]})
print(f"    Status : {settle['status']}")
print(f"    Message: {settle['message']}")

time.sleep(0.5)  # let worker process

# ── Ledger Audit ──────────────────────────────────────────────────────────────
print("\n[8] Ledger Audit (Double-Entry Verification)...")
audit = get("/ledger/audit")
print(f"    Total entries    : {audit['total_entries']}")
print(f"    Total journals   : {audit['total_journals']}")
print(f"    Total debits     : Rs.{audit['total_debits_dollars']:.2f}")
print(f"    Total credits    : Rs.{audit['total_credits_dollars']:.2f}")
print(f"    Balanced         : {audit['is_balanced']}  (sum DR == sum CR)")
print(f"    Chain integrity  : {audit['chain_integrity_ok']}  (SHA-256 hash chain OK)")
print(f"    Chain errors     : {audit['chain_errors']}")

print()
print("=" * 55)
print("  Demo complete! Open http://localhost:8000/docs")
print("  to explore further via the Swagger UI.")
print("=" * 55)


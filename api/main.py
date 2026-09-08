"""
FastAPI Application — Distributed Expense Settlement Engine
===========================================================
Entry point for the REST API server.

Startup lifecycle:
    1. Initialize cache (Redis or in-memory fallback)
    2. Start outbox relay daemon
    3. Start settlement worker

Shutdown lifecycle:
    1. Stop settlement worker
    2. Drain outbox

Run with:
    python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
"""
import asyncio
import logging
import sys
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.routes import users, groups, expenses, ledger
from cache.redis_client import init_cache
from messaging.outbox import get_outbox
from worker.worker import get_worker

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────
REDIS_URL    = os.getenv("REDIS_URL",    "redis://localhost:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./expense_engine.db")
ENV          = os.getenv("ENV",          "development")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup and shutdown hooks."""
    log.info("=== Expense Settlement Engine starting (ENV=%s) ===", ENV)

    # 1. Initialize cache
    await init_cache(redis_url=REDIS_URL if ENV != "development" else None)
    log.info("Cache initialized.")

    # 2. Start outbox relay daemon
    outbox      = get_outbox()
    relay_task  = asyncio.create_task(outbox.start_relay_daemon(interval_seconds=0.25))
    log.info("Outbox relay daemon started.")

    # 3. Start settlement worker
    worker = get_worker()
    await worker.start()
    log.info("Settlement worker started.")

    log.info("=== Expense Settlement Engine ready ===")

    yield  # Application runs here

    # Shutdown
    log.info("=== Expense Settlement Engine shutting down ===")
    await worker.stop()
    relay_task.cancel()
    try:
        await relay_task
    except asyncio.CancelledError:
        pass
    log.info("Shutdown complete.")


# ─── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "Distributed Expense Settlement Engine",
    description = (
        "A high-performance backend for multi-party expense sharing and debt settlement. "
        "Features O(N log N) Max-Heap debt simplification, PostgreSQL double-entry accounting "
        "with Optimistic Concurrency Control, and a decoupled Redis Streams event architecture."
    ),
    version     = "1.0.0",
    lifespan    = lifespan,
    docs_url    = "/docs",
    redoc_url   = "/redoc",
)

# ─── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ─── Exception Handlers ───────────────────────────────────────────────────────
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"error": "Bad Request", "detail": str(exc)})

@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    log.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content={"error": "Internal Server Error", "detail": str(exc)})

# ─── Routers ──────────────────────────────────────────────────────────────────
app.include_router(users.router)
app.include_router(groups.router)
app.include_router(expenses.router)
app.include_router(ledger.router)


@app.get("/", tags=["Root"])
async def root():
    return {
        "service":     "Distributed Expense Settlement Engine",
        "version":     "1.0.0",
        "docs":        "/docs",
        "health":      "/api/v1/health",
        "audit":       "/api/v1/ledger/audit",
        "algorithm":   "O(N log N) Max-Heap Debt Simplification",
        "ledger":      "Double-Entry Accounting with OCC",
        "message_bus": "Redis Streams (InMemoryQueue fallback)",
        "cache":       "Redis (InMemoryCache fallback)",
    }


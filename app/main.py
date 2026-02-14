"""FastAPI application for Principal Miner Rewards.

Entry point: uvicorn app.main:app --host 0.0.0.0 --port 8100
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import create_all_tables
from .routes import admin, claim, miner_info, rewards

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    logger.info("Starting Principal Miner Rewards API")
    logger.info(f"  MINER_HOTKEY: {settings.miner_hotkey[:16]}...")
    logger.info(f"  AGGREGATOR_HOTKEY: {settings.aggregator_hotkey[:16]}...")
    logger.info(f"  COMMISSION_RATE: {settings.commission_rate}")
    logger.info(f"  HOME_EVM_ADDRESSES: {len(settings.home_evm_addresses)} addresses")
    logger.info(f"  BT_NETWORK: {settings.bt_network}")

    # Create tables if they don't exist (dev convenience; use Alembic in prod)
    try:
        await create_all_tables()
        logger.info("Database tables verified/created")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")

    yield

    # Shutdown
    logger.info("Shutting down Principal Miner Rewards API")


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Principal Miner Rewards",
    description="Backend for your Cartha principal miner serving federated miners",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS - configurable via CORS_ORIGINS env var (defaults to ["*"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check."""
    return {"status": "ok"}


app.include_router(rewards.router)
app.include_router(claim.router)
app.include_router(miner_info.router)
app.include_router(admin.router)

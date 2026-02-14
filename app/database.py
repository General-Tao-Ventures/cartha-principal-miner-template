"""PostgreSQL database engine, async session factory, and helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, AsyncGenerator

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .models import Base, Claim, ClaimStatus, RewardEntry

# ─── Engine & Session ─────────────────────────────────────────────────────────

_engine = None
_session_factory = None


def get_engine():
    """Get or create the async engine (lazy initialization)."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory():
    """Get or create the async session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def dispose_engine():
    """Dispose the engine (for tests that use multiple asyncio.run calls)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def reset_engine():
    """Drop engine/session references so the next step creates a fresh engine.

    Use between test steps where each step calls asyncio.run() separately.
    We intentionally do NOT call dispose/close since the old event loop is
    already gone; just null out and let GC handle stale connections.
    """
    global _engine, _session_factory
    _engine = None
    _session_factory = None


# Backward-compatible aliases
@property
def engine():
    return get_engine()


def async_session_factory():
    return get_session_factory()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


# ─── Table Checks ─────────────────────────────────────────────────────────────


async def check_tables_exist() -> list[str]:
    """Return list of our tables that exist in the database."""
    async with get_engine().connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' "
                "AND tablename IN ('sweeps', 'reward_entries', 'claims')"
            )
        )
        return [row[0] for row in result.fetchall()]


async def create_all_tables() -> None:
    """Create all tables (for development/testing). Use Alembic in production."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ─── Balance Computation ─────────────────────────────────────────────────────


async def compute_balance_in_session(
    session: AsyncSession, evm_address: str
) -> dict[str, Any]:
    """Compute available balance using the caller's session/transaction.

    This ensures the balance check participates in the same transaction
    (and advisory lock scope) as the caller, preventing race conditions
    when used inside the claim flow.

    Balance = SUM(net_alpha from reward_entries)
            - SUM(amount_alpha from claims WHERE status IN (processing, completed))

    Returns:
        Dict with total_earned, total_claimed, available
    """
    addr = evm_address.lower().strip()

    # Total earned
    earned_result = await session.execute(
        select(func.coalesce(func.sum(RewardEntry.net_alpha), 0)).where(
            RewardEntry.evm_address == addr
        )
    )
    total_earned = Decimal(str(earned_result.scalar_one()))

    # Total claimed (processing + completed)
    claimed_result = await session.execute(
        select(func.coalesce(func.sum(Claim.amount_alpha), 0)).where(
            Claim.evm_address == addr,
            Claim.status.in_([ClaimStatus.PROCESSING, ClaimStatus.COMPLETED]),
        )
    )
    total_claimed = Decimal(str(claimed_result.scalar_one()))

    available = total_earned - total_claimed

    return {
        "total_earned": float(total_earned),
        "total_claimed": float(total_claimed),
        "available": float(available),
    }


async def compute_balance(evm_address: str) -> dict[str, Any]:
    """Compute available balance for a single EVM address.

    Uses its own session. For read-only routes (rewards, admin).
    For the claim flow, use compute_balance_in_session() instead
    to participate in the same transaction/advisory lock scope.

    Returns:
        Dict with total_earned, total_claimed, available
    """
    async with get_session_factory()() as session:
        return await compute_balance_in_session(session, evm_address)


async def compute_balances() -> dict[str, dict[str, Any]]:
    """Compute available balances for ALL EVM addresses with reward entries.

    Returns:
        Dict mapping evm_address -> {total_earned, total_claimed, available}
    """
    async with get_session_factory()() as session:
        # Get all unique addresses with rewards
        addr_result = await session.execute(
            select(RewardEntry.evm_address).distinct()
        )
        addresses = [row[0] for row in addr_result.fetchall()]

    # Compute balance for each
    balances: dict[str, dict[str, Any]] = {}
    for addr in addresses:
        balances[addr] = await compute_balance(addr)

    return balances

"""Public miner info endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_session
from ..models import Claim, ClaimStatus, RewardEntry, Sweep

router = APIRouter(prefix="/api", tags=["miner"])


async def _compute_apy(
    session: AsyncSession,
    hours: int,
) -> dict[str, Any]:
    """Compute annualized APY from sweep earnings over the given window.

    Returns dict with alpha_earned, sweep_count, daily_rate, apy.
    APY = ((1 + daily_rate) ^ 365 - 1) * 100
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    earned_result = await session.execute(
        select(func.coalesce(func.sum(Sweep.alpha_amount), 0)).where(
            Sweep.success == True,  # noqa: E712
            Sweep.created_at >= cutoff,
        )
    )
    alpha_earned = float(earned_result.scalar_one())

    count_result = await session.execute(
        select(func.count(Sweep.id)).where(
            Sweep.success == True,  # noqa: E712
            Sweep.created_at >= cutoff,
        )
    )
    sweep_count = count_result.scalar_one()

    # Get total stake from all-time swept (proxy for average stake)
    # A more accurate approach would query the chain, but this avoids
    # adding chain calls to every API request
    total_swept_result = await session.execute(
        select(func.coalesce(func.sum(Sweep.alpha_amount), 0)).where(
            Sweep.success == True,  # noqa: E712
        )
    )
    total_swept = float(total_swept_result.scalar_one())

    days = hours / 24.0
    daily_alpha = alpha_earned / days if days > 0 else 0.0

    return {
        "alpha_earned": alpha_earned,
        "sweep_count": sweep_count,
        "daily_alpha": daily_alpha,
        "window_hours": hours,
    }


@router.get("/miner-info")
async def get_miner_info(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Public principal miner info: commission rate, positions, totals, APY."""

    # Total ALPHA swept (all time)
    swept_result = await session.execute(
        select(func.coalesce(func.sum(Sweep.alpha_amount), 0)).where(
            Sweep.success == True  # noqa: E712
        )
    )
    total_swept = float(swept_result.scalar_one())

    # Total sweeps
    sweep_count_result = await session.execute(
        select(func.count(Sweep.id)).where(Sweep.success == True)  # noqa: E712
    )
    total_sweeps = sweep_count_result.scalar_one()

    # Unique federated miner addresses
    addr_result = await session.execute(
        select(func.count(func.distinct(RewardEntry.evm_address)))
    )
    unique_miners = addr_result.scalar_one()

    # Total net ALPHA distributed
    net_result = await session.execute(
        select(func.coalesce(func.sum(RewardEntry.net_alpha), 0))
    )
    total_distributed = float(net_result.scalar_one())

    # Total commission
    commission_result = await session.execute(
        select(func.coalesce(func.sum(RewardEntry.commission_alpha), 0))
    )
    total_commission = float(commission_result.scalar_one())

    # Completed claims count and total ALPHA claimed
    claims_count_result = await session.execute(
        select(func.count(Claim.id)).where(
            Claim.status == ClaimStatus.COMPLETED
        )
    )
    total_claims = claims_count_result.scalar_one()

    claims_alpha_result = await session.execute(
        select(func.coalesce(func.sum(Claim.amount_alpha), 0)).where(
            Claim.status == ClaimStatus.COMPLETED
        )
    )
    total_alpha_claimed = float(claims_alpha_result.scalar_one())

    # APY calculations (24h and 7d windows)
    apy_24h = await _compute_apy(session, hours=24)
    apy_7d = await _compute_apy(session, hours=168)

    return {
        "miner_hotkey": settings.miner_hotkey,
        "miner_name": settings.miner_name,
        "miner_description": settings.miner_description,
        "miner_website": settings.miner_website,
        "miner_discord": settings.miner_discord,
        "miner_logo_url": settings.miner_logo_url,
        "aggregator_hotkey": settings.aggregator_hotkey,
        "commission_rate": settings.commission_rate,
        "bt_network": settings.bt_network,
        "total_alpha_swept": total_swept,
        "total_sweeps": total_sweeps,
        "total_alpha_distributed": total_distributed,
        "total_commission": total_commission,
        "unique_miners": unique_miners,
        "total_claims": total_claims,
        "total_alpha_claimed": total_alpha_claimed,
        "apy_24h": apy_24h,
        "apy_7d": apy_7d,
    }

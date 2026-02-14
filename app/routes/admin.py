"""Admin endpoints for manual operations."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import compute_balances, get_session
from ..models import RewardEntry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ─── Admin Auth ───────────────────────────────────────────────────────────────


async def require_admin_key(
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
) -> None:
    """FastAPI dependency: require a valid admin API key via X-Admin-Key header.

    If ADMIN_API_KEY is not configured (empty string), admin endpoints are
    disabled entirely for safety.
    """
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=403,
            detail="Admin endpoints are disabled (ADMIN_API_KEY not configured)",
        )
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")


@router.post("/run-scoring")
async def manual_run_scoring(
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Manually trigger a sweep + score cycle.

    WARNING: This triggers a real sweep if not in dry-run mode.
    Should be protected by API key or IP whitelist in production.
    """
    from ..jobs.sweep_and_score import record_sweep_and_rewards
    from ..transfer import get_subtensor, sweep_to_aggregator
    from ..config import settings, SUBNET_NETUID

    try:
        subtensor = get_subtensor(network=settings.bt_network)
        current_block = subtensor.get_current_block()
    except Exception as e:
        return {"error": f"Cannot connect to BT chain: {e}"}

    # For manual runs, use current block as epoch identifier
    # In production, this would be triggered by the epoch monitor
    try:
        from ..wallet import load_wallet

        wallet = load_wallet()

        success, amount = sweep_to_aggregator(wallet=wallet)
    except Exception as e:
        return {"error": f"Sweep failed: {e}"}

    if not success:
        return {"error": "Sweep returned failure"}

    try:
        result = await record_sweep_and_rewards(
            bt_epoch_block=current_block,
            alpha_amount=amount,
        )
        return {
            "status": "completed",
            "block": current_block,
            "alpha_swept": amount,
            **result,
        }
    except Exception as e:
        return {"error": f"Scoring failed: {e}"}


@router.get("/summary")
async def rewards_summary(
    _: None = Depends(require_admin_key),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Current rewards summary for all federated miners.

    Used by Slack daily summary notifications.
    """
    balances = await compute_balances()

    # Sort by available balance descending
    sorted_miners = sorted(
        balances.items(),
        key=lambda x: x[1]["available"],
        reverse=True,
    )

    total_owed = sum(b["available"] for b in balances.values())
    total_earned = sum(b["total_earned"] for b in balances.values())
    total_claimed = sum(b["total_claimed"] for b in balances.values())

    return {
        "total_miners": len(balances),
        "total_earned": total_earned,
        "total_claimed": total_claimed,
        "total_owed": total_owed,
        "miners": [
            {
                "evm_address": addr,
                **bal,
            }
            for addr, bal in sorted_miners
        ],
    }

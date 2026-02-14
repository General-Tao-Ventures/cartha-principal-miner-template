"""Reward and claim query endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_session, compute_balance, compute_balances
from ..models import RewardEntry, Claim, ClaimStatus, Sweep

router = APIRouter(prefix="/api", tags=["rewards"])


@router.get("/rewards/{evm_address}")
async def get_rewards(
    evm_address: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get reward summary for an EVM address.

    Returns total earned, total claimed, available balance, and recent entries.
    """
    addr = evm_address.lower().strip()
    balance = await compute_balance(addr)

    # Recent reward entries (last 50)
    result = await session.execute(
        select(RewardEntry, Sweep.bt_epoch_block, Sweep.created_at.label("sweep_time"))
        .join(Sweep, RewardEntry.sweep_id == Sweep.id)
        .where(RewardEntry.evm_address == addr)
        .order_by(desc(RewardEntry.created_at))
        .limit(50)
    )
    rows = result.all()

    entries = []
    for entry, block, sweep_time in rows:
        entries.append({
            "id": entry.id,
            "sweep_id": entry.sweep_id,
            "bt_epoch_block": block,
            "gross_alpha": float(entry.gross_alpha),
            "commission_alpha": float(entry.commission_alpha),
            "net_alpha": float(entry.net_alpha),
            "share": float(entry.share),
            "is_home": entry.is_home,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "sweep_time": sweep_time.isoformat() if sweep_time else None,
        })

    # ── Claim cooldown info ─────────────────────────────────────────────
    cooldown = settings.claim_cooldown_seconds
    last_claim_at: str | None = None
    can_claim_at: str | None = None
    cooldown_remaining: int = 0

    if cooldown > 0:
        last_claim_result = await session.execute(
            select(Claim.created_at)
            .where(
                Claim.evm_address == addr,
                Claim.status.in_([ClaimStatus.PROCESSING, ClaimStatus.COMPLETED]),
            )
            .order_by(desc(Claim.created_at))
            .limit(1)
        )
        last_claim_row = last_claim_result.scalar_one_or_none()
        if last_claim_row is not None:
            last_claim_at = last_claim_row.isoformat()
            from datetime import timedelta
            claim_available_at = last_claim_row + timedelta(seconds=cooldown)
            now = datetime.now(timezone.utc)
            if claim_available_at > now:
                can_claim_at = claim_available_at.isoformat()
                cooldown_remaining = int((claim_available_at - now).total_seconds())

    return {
        "evm_address": addr,
        **balance,
        "recent_entries": entries,
        "claim_cooldown_seconds": cooldown,
        "last_claim_at": last_claim_at,
        "can_claim_at": can_claim_at,
        "cooldown_remaining": cooldown_remaining,
    }


@router.get("/federated-miners")
async def get_federated_miners(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List all federated miners with their balances and latest reward info."""
    balances = await compute_balances()

    # Get latest reward entry per address for share/score info
    miners = []
    for addr, bal in sorted(balances.items(), key=lambda x: x[1]["total_earned"], reverse=True):
        # Get the most recent reward entry for this address
        latest_result = await session.execute(
            select(RewardEntry)
            .where(RewardEntry.evm_address == addr)
            .order_by(desc(RewardEntry.created_at))
            .limit(1)
        )
        latest = latest_result.scalar_one_or_none()

        miners.append({
            "evm_address": addr,
            "total_earned": bal["total_earned"],
            "total_claimed": bal["total_claimed"],
            "available": bal["available"],
            "is_home": latest.is_home if latest else False,
            "latest_share": float(latest.share) if latest else 0.0,
        })

    return {
        "total_miners": len(miners),
        "miners": miners,
    }


@router.get("/claims/{evm_address}")
async def get_claims(
    evm_address: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get claim history for an EVM address."""
    addr = evm_address.lower().strip()

    result = await session.execute(
        select(Claim)
        .where(Claim.evm_address == addr)
        .order_by(desc(Claim.created_at))
        .limit(50)
    )
    claims = result.scalars().all()

    return {
        "evm_address": addr,
        "claims": [
            {
                "id": c.id,
                "bt_coldkey": c.bt_coldkey,
                "amount_alpha": float(c.amount_alpha),
                "status": c.status.value if isinstance(c.status, ClaimStatus) else c.status,
                "tx_hash": c.tx_hash,
                "error": c.error,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "processed_at": c.processed_at.isoformat() if c.processed_at else None,
            }
            for c in claims
        ],
    }

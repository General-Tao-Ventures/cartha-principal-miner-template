"""Per-epoch sweep to aggregator + score positions + record rewards.

Called by the epoch monitor whenever a new BT epoch boundary is reached.
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal, ROUND_DOWN
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import settings
from ..database import get_session_factory
from ..models import RewardEntry, Sweep
from ..scoring import score_positions_segmented
from ..verifier_client import fetch_scoring_positions

logger = logging.getLogger(__name__)


async def record_sweep_and_rewards(
    bt_epoch_block: int,
    alpha_amount: float,
    *,
    dry_run: bool = False,
    slot: int | None = None,
) -> dict[str, Any]:
    """Record a sweep and calculate + store reward entries.

    1. Insert sweep record (idempotent on bt_epoch_block)
    2. Fetch and score positions
    3. Insert reward entries (idempotent on sweep_id + evm_address)

    Args:
        bt_epoch_block: The BT block that triggered this sweep
        alpha_amount: ALPHA amount swept (the epoch's earnings)
        dry_run: If True, still record to DB but won't affect real chain
        slot: Miner slot/UID (will resolve if not provided)

    Returns:
        Dict with sweep_id, entries_created, per_address rewards, etc.
    """
    result: dict[str, Any] = {
        "sweep_id": None,
        "entries_created": 0,
        "total_commission": 0.0,
        "per_address": {},
    }

    async with get_session_factory()() as session:
        # ── 1. Insert sweep record (idempotent) ──────────────────────────
        existing = await session.execute(
            select(Sweep).where(Sweep.bt_epoch_block == bt_epoch_block)
        )
        existing_sweep = existing.scalar_one_or_none()

        if existing_sweep:
            # Already processed this epoch block
            result["sweep_id"] = existing_sweep.id
            result["entries_created"] = 0
            return result

        sweep = Sweep(
            bt_epoch_block=bt_epoch_block,
            alpha_amount=Decimal(str(alpha_amount)),
            success=True,
        )
        session.add(sweep)
        await session.flush()  # Get the ID
        result["sweep_id"] = sweep.id

        # ── 2. Skip if nothing earned ────────────────────────────────────
        if alpha_amount <= 0:
            await session.commit()
            return result

        # ── 3. Fetch and score positions ─────────────────────────────────
        if slot is None:
            try:
                from ..transfer import resolve_slot
                slot = resolve_slot(settings.miner_hotkey, network=settings.bt_network)
            except Exception as e:
                logger.warning(f"Could not resolve slot: {e}")
                sweep.error = f"Could not resolve slot: {e}"
                await session.commit()
                return result

        positions = fetch_scoring_positions(
            hotkey=settings.miner_hotkey, slot=slot
        )

        if not positions:
            logger.info("No active positions to score")
            await session.commit()
            return result

        segmented = score_positions_segmented(
            positions=positions,
            total_alpha=alpha_amount,
            home_evm_addresses=settings.home_evm_addresses,
            commission_rate=settings.commission_rate,
        )

        result["total_commission"] = segmented.commission_alpha

        # ── 4. Insert reward entries ─────────────────────────────────────
        entries_created = 0
        all_scored = segmented.home_positions + segmented.guest_positions

        # Group by EVM address (aggregate multiple positions per address)
        addr_rewards: dict[str, dict[str, Any]] = {}
        for sp in all_scored:
            addr = sp.evm_address.lower()
            if addr not in addr_rewards:
                addr_rewards[addr] = {
                    "gross_alpha": 0.0,
                    "commission_alpha": 0.0,
                    "net_alpha": 0.0,
                    "raw_score": 0.0,
                    "share": 0.0,
                    "is_home": addr in {
                        a.lower() for a in (settings.home_evm_addresses or [])
                    },
                }
            r = addr_rewards[addr]
            r["raw_score"] += sp.raw_score
            r["share"] += sp.share  # Will be re-normalized below

        # Floor to 2 decimal places -- ensures we never owe more than we have
        def floor2(val: float) -> float:
            return math.floor(val * 100) / 100.0

        # Calculate gross/commission/net per address (all floored to 2dp)
        total_score = segmented.total_score
        for addr, r in addr_rewards.items():
            if total_score > 0:
                addr_share = r["raw_score"] / total_score
            else:
                addr_share = 0.0

            r["share"] = addr_share
            r["gross_alpha"] = floor2(alpha_amount * addr_share)

            if r["is_home"]:
                r["commission_alpha"] = 0.0
            else:
                r["commission_alpha"] = floor2(r["gross_alpha"] * settings.commission_rate)

            r["net_alpha"] = floor2(r["gross_alpha"] - r["commission_alpha"])

        # Insert entries
        TWO_DP = Decimal("0.01")
        for addr, r in addr_rewards.items():
            entry = RewardEntry(
                sweep_id=sweep.id,
                evm_address=addr,
                gross_alpha=Decimal(str(r["gross_alpha"])).quantize(TWO_DP, rounding=ROUND_DOWN),
                commission_alpha=Decimal(str(r["commission_alpha"])).quantize(TWO_DP, rounding=ROUND_DOWN),
                net_alpha=Decimal(str(r["net_alpha"])).quantize(TWO_DP, rounding=ROUND_DOWN),
                raw_score=Decimal(str(r["raw_score"])),
                share=Decimal(str(r["share"])),
                is_home=r["is_home"],
            )
            session.add(entry)
            entries_created += 1
            result["per_address"][addr] = r["net_alpha"]

        result["entries_created"] = entries_created
        await session.commit()

    return result

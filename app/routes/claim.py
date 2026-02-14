"""Claim submission endpoint.

Concurrency-safe three-phase claim flow:
  Phase 1 – Atomic balance check + PROCESSING claim creation (per-address advisory lock)
  Phase 2 – On-chain transfer (global chain-transfer advisory lock, runs in thread pool)
  Phase 3 – Status update (COMPLETED / FAILED)
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text, update

from ..auth import verify_claim_signature
from ..config import settings
from ..database import compute_balance_in_session, get_session_factory
from ..models import Claim, ClaimStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["claims"])

# Advisory lock keys used by both the claim endpoint and the epoch monitor.
# Key 0 serializes ALL blockchain transactions (sweeps + claims) to prevent
# nonce conflicts from the shared wallet.
GLOBAL_CHAIN_LOCK_KEY = 0


def _log(msg: str) -> None:
    """Log to both logger AND print (bittensor suppresses logger)."""
    logger.info(msg)
    print(f"[CLAIM] {msg}", flush=True)


def _addr_lock_key(evm_address: str) -> int:
    """Derive a per-address PostgreSQL advisory lock key (positive int32)."""
    return hash(evm_address) & 0x7FFFFFFF


def _load_wallet():
    """Load the BT wallet (env var first, GCP as optional fallback)."""
    from ..wallet import load_wallet

    wallet = load_wallet()
    _log(f"Wallet loaded: {wallet.name}")
    return wallet


class ClaimRequest(BaseModel):
    """Request body for submitting a claim."""

    evm_address: str
    bt_coldkey: str
    amount_alpha: float
    signature: str
    message: str
    timestamp: int


@router.post("/claim")
async def submit_claim(req: ClaimRequest) -> dict[str, Any]:
    """Submit a claim to withdraw accumulated rewards.

    Three-phase concurrency-safe flow:
      Phase 1 – Acquire per-address advisory lock, check balance, create
                PROCESSING claim, COMMIT (makes claim visible, releases lock).
      Phase 2 – Acquire global chain-transfer lock, run transfer in thread
                pool so the event loop stays responsive, release lock.
      Phase 3 – Update claim status to COMPLETED or FAILED.
    """
    addr = req.evm_address.lower().strip()
    _log(f"Claim request: {addr[:10]}... -> {req.bt_coldkey[:10]}... for {req.amount_alpha} ALPHA")

    # ── 1. Verify signature (stateless, no DB needed) ─────────────────────
    _log("[1/5] Verifying EVM signature...")
    try:
        verify_claim_signature(
            evm_address=addr,
            signature=req.signature,
            message=req.message,
        )
        _log("[1/5] Signature verified OK")
    except ValueError as e:
        _log(f"[1/5] Signature INVALID: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")

    # ── Validate basic request fields before acquiring locks ───────────────
    if req.amount_alpha <= 0:
        _log("REJECTED: amount <= 0")
        raise HTTPException(status_code=400, detail="Claim amount must be positive")

    if req.amount_alpha < settings.min_claim_alpha:
        _log(f"REJECTED: below minimum ({settings.min_claim_alpha})")
        raise HTTPException(
            status_code=400,
            detail=f"Below minimum claim amount ({settings.min_claim_alpha} ALPHA)",
        )

    # ══════════════════════════════════════════════════════════════════════
    #  PHASE 1 – Atomic balance check + PROCESSING claim creation
    #  Uses pg_advisory_xact_lock(addr_key) so concurrent claims for the
    #  same address are serialized.  The lock is released on COMMIT, at
    #  which point the PROCESSING claim becomes visible to other sessions.
    # ══════════════════════════════════════════════════════════════════════
    _log("[2/5] Acquiring per-address lock & checking balance...")
    addr_key = _addr_lock_key(addr)

    async with get_session_factory()() as session:
        # Acquire per-address advisory lock (blocks concurrent claims for same address)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": addr_key}
        )

        # ── Rate limit: reject if a claim was created in the last N seconds ──
        cooldown = settings.claim_cooldown_seconds
        if cooldown > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=cooldown)
            recent = await session.execute(
                select(Claim.id).where(
                    Claim.evm_address == addr,
                    Claim.status.in_([ClaimStatus.PROCESSING, ClaimStatus.COMPLETED]),
                    Claim.created_at >= cutoff,
                )
            )
            if recent.scalar_one_or_none() is not None:
                _log(f"REJECTED: cooldown ({cooldown}s) not elapsed")
                raise HTTPException(
                    status_code=429,
                    detail=f"Please wait at least {cooldown}s between claims",
                )

        # ── Balance check (same session = sees all committed PROCESSING claims) ──
        balance = await compute_balance_in_session(session, addr)
        available = balance["available"]
        _log(
            f"[2/5] Balance: earned={balance['total_earned']:.2f}, "
            f"claimed={balance['total_claimed']:.2f}, available={available:.2f}"
        )

        if req.amount_alpha > available:
            _log(f"[2/5] REJECTED: insufficient balance (have {available:.2f}, want {req.amount_alpha:.2f})")
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient balance: available={available:.2f}, requested={req.amount_alpha:.2f}",
            )

        # ── Create claim as PROCESSING ──
        claim = Claim(
            evm_address=addr,
            bt_coldkey=req.bt_coldkey,
            amount_alpha=Decimal(str(req.amount_alpha)),
            status=ClaimStatus.PROCESSING,
            signature=req.signature,
        )
        session.add(claim)
        await session.commit()  # Advisory lock released; PROCESSING now visible to all
        claim_id = claim.id
        _log(f"[3/5] Claim record created & committed: id={claim_id}")

    # ══════════════════════════════════════════════════════════════════════
    #  PHASE 2 – On-chain transfer
    #  Acquires global advisory lock (key=0) to serialize ALL chain
    #  transactions (claims + sweeps share the same wallet/nonces).
    #  The actual BT SDK call runs in a thread pool via asyncio.to_thread
    #  so the event loop remains responsive.
    # ══════════════════════════════════════════════════════════════════════
    _log("[4/5] Loading wallet...")
    wallet = _load_wallet()

    tx_hash: str | None = None
    error_msg: str | None = None
    status = ClaimStatus.COMPLETED

    async with get_session_factory()() as lock_session:
        # Acquire global chain-transfer lock (blocks until sweep / other claim finishes)
        _log("[4/5] Acquiring global chain-transfer lock...")
        await lock_session.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": GLOBAL_CHAIN_LOCK_KEY}
        )
        try:
            _log(f"[4/5] Transferring {req.amount_alpha} ALPHA to {req.bt_coldkey[:16]}...")
            from ..transfer import transfer_claim as _transfer_claim

            tx_hash = await asyncio.to_thread(
                _transfer_claim,
                recipient_coldkey=req.bt_coldkey,
                alpha_amount=req.amount_alpha,
                wallet=wallet,
            )
            _log(f"[4/5] Transfer COMPLETED: tx={tx_hash}")
        except Exception as e:
            error_msg = str(e)
            tb = traceback.format_exc()
            status = ClaimStatus.FAILED
            _log(f"[4/5] Transfer FAILED: {error_msg}")
            print(f"[CLAIM] Traceback:\n{tb}", flush=True)
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": GLOBAL_CHAIN_LOCK_KEY}
            )

    # ══════════════════════════════════════════════════════════════════════
    #  PHASE 3 – Update claim status
    # ══════════════════════════════════════════════════════════════════════
    async with get_session_factory()() as session:
        now = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "status": status,
            "processed_at": now,
        }
        if tx_hash is not None:
            values["tx_hash"] = tx_hash
        if error_msg is not None:
            values["error"] = error_msg

        await session.execute(
            update(Claim).where(Claim.id == claim_id).values(**values)
        )
        await session.commit()

    _log(f"[5/5] Claim {claim_id} finalized: status={status.value}")

    # ── Slack notification (best-effort) ──────────────────────────────────
    try:
        from ..slack_notifier import SlackNotifier

        notifier = SlackNotifier(
            webhook_url=settings.slack_webhook_url,
            error_webhook_url=settings.slack_error_webhook_url,
        )
        notifier.notify_claim(
            evm_address=addr,
            bt_coldkey=req.bt_coldkey,
            amount=req.amount_alpha,
            tx_hash=tx_hash,
            status=status.value,
        )
    except Exception:
        pass  # Don't fail the claim if Slack fails

    # ── Response ──────────────────────────────────────────────────────────
    if status == ClaimStatus.FAILED:
        raise HTTPException(status_code=500, detail=f"Transfer failed: {error_msg}")

    return {
        "claim_id": claim_id,
        "status": status.value,
        "tx_hash": tx_hash,
        "amount_alpha": req.amount_alpha,
    }

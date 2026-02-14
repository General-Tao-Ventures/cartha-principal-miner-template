#!/usr/bin/env python3
"""
End-to-End Test Script for Internal Principal Rewards System.

This script serves as both the test suite and the development progress tracker.
Each step maps to a backend component. Run at any time to see current status.

Usage:
    python tests/test_e2e.py              # dry-run mode (default, no real BT ops)
    python tests/test_e2e.py --live       # live mode (real BT chain operations)
    python tests/test_e2e.py --step 7     # run only step 7
    python tests/test_e2e.py --from 5     # run from step 5 onwards
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ─── Logging Helpers ──────────────────────────────────────────────────────────


class StepStatus(Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class StepResult:
    step: int
    name: str
    status: StepStatus
    duration_s: float = 0.0
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def ts() -> str:
    """Current UTC timestamp for logging."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str, indent: int = 0) -> None:
    """Print a timestamped log line."""
    prefix = "  " * indent
    print(f"[{ts()}] {prefix}{msg}")


def log_header(step: int, name: str) -> None:
    """Print a step header."""
    print()
    print(f"[{ts()}] {'═' * 60}")
    print(f"[{ts()}] ═══ STEP {step:2d}: {name}")
    print(f"[{ts()}] {'═' * 60}")


def log_result(result: StepResult) -> None:
    """Print a step result."""
    symbol = {"PASSED": "✓", "FAILED": "✗", "SKIPPED": "⊘"}[result.status.value]
    print(f"[{ts()}] {symbol} STEP {result.step} {result.status.value} ({result.duration_s:.1f}s)")
    if result.error:
        print(f"[{ts()}]   Error: {result.error}")


# ─── Step Runner ──────────────────────────────────────────────────────────────


def run_step(step: int, name: str, fn, *, dry_run: bool = True) -> StepResult:
    """Run a single test step with timing and error handling."""
    log_header(step, name)
    start = time.time()

    # Reset DB engine between steps to avoid event loop conflicts
    # (each step calls asyncio.run() which creates/closes its own loop)
    try:
        from app.database import reset_engine
        reset_engine()
    except Exception:
        pass

    try:
        result_details = fn(dry_run=dry_run)
        duration = time.time() - start

        if result_details is None:
            result_details = {}

        result = StepResult(
            step=step,
            name=name,
            status=StepStatus.PASSED,
            duration_s=duration,
            details=result_details,
        )
    except NotImplementedError as e:
        duration = time.time() - start
        log(f"SKIP: {e}", indent=1)
        result = StepResult(
            step=step,
            name=name,
            status=StepStatus.SKIPPED,
            duration_s=duration,
            error=str(e),
        )
    except Exception as e:
        duration = time.time() - start
        log(f"FAIL: {e}", indent=1)
        traceback.print_exc()
        result = StepResult(
            step=step,
            name=name,
            status=StepStatus.FAILED,
            duration_s=duration,
            error=str(e),
        )

    log_result(result)
    return result


# ─── Step Implementations ─────────────────────────────────────────────────────
# Each step is a function that receives dry_run=True/False.
# Raise NotImplementedError("not yet implemented") to skip.
# Return a dict of details for logging.
# Raise any other exception to fail.


def step_01_config_loading(*, dry_run: bool) -> dict:
    """Load .env, validate all required vars, print config summary."""
    try:
        from app.config import settings
    except ImportError:
        raise NotImplementedError("app.config module not yet created")

    log(f"DATABASE_URL: {settings.database_url[:30]}...", indent=1)
    log(f"VERIFIER_URL: {settings.verifier_url}", indent=1)
    log(f"MINER_HOTKEY: {settings.miner_hotkey}", indent=1)
    log(f"MINER_COLDKEY: {settings.miner_coldkey}", indent=1)
    log(f"AGGREGATOR_HOTKEY: {settings.aggregator_hotkey}", indent=1)
    log(f"HOME_EVM_ADDRESSES: {settings.home_evm_addresses}", indent=1)
    log(f"COMMISSION_RATE: {settings.commission_rate}", indent=1)
    log(f"BT_NETWORK: {settings.bt_network}", indent=1)
    log(f"POLL_INTERVAL: {settings.poll_interval}s", indent=1)

    # Validate required fields
    assert settings.database_url, "DATABASE_URL is required"
    assert settings.miner_hotkey, "MINER_HOTKEY is required"
    assert settings.miner_coldkey, "MINER_COLDKEY is required"
    assert settings.aggregator_hotkey, "AGGREGATOR_HOTKEY is required"
    assert 0.0 <= settings.commission_rate <= 1.0, "COMMISSION_RATE must be 0-1"

    return {"commission_rate": settings.commission_rate}


def step_02_database_connection(*, dry_run: bool) -> dict:
    """Connect to PostgreSQL, verify tables exist."""
    try:
        from app.database import engine, check_tables_exist
    except ImportError:
        raise NotImplementedError("app.database module not yet created")

    tables = asyncio.run(check_tables_exist())
    log(f"Tables found: {tables}", indent=1)

    expected = {"sweeps", "reward_entries", "claims"}
    missing = expected - set(tables)
    assert not missing, f"Missing tables: {missing}"

    log(f"All {len(expected)} tables exist", indent=1)
    return {"tables": tables}


def step_03_gcp_wallet_unlock(*, dry_run: bool) -> dict:
    """Fetch wallet password from GCP, unlock wallet."""
    try:
        from app.wallet import load_wallet_with_gcp_secret
    except ImportError:
        raise NotImplementedError("app.wallet module not yet created")

    try:
        from app.config import settings
    except ImportError:
        raise NotImplementedError("app.config module not yet created")

    if dry_run and not settings.gcp_secret_id:
        log("DRY-RUN: Skipping GCP secret fetch (no GCP_SECRET_ID set)", indent=1)
        raise NotImplementedError("GCP_SECRET_ID not configured for dry-run")

    wallet = load_wallet_with_gcp_secret()
    coldkey_address = wallet.coldkey.ss58_address
    log(f"Wallet name: {wallet.name}", indent=1)
    log(f"Coldkey unlocked: {coldkey_address[:10]}...{coldkey_address[-6:]}", indent=1)
    log(f"Match MINER_COLDKEY: {'YES' if coldkey_address == settings.miner_coldkey else 'NO'}", indent=1)

    assert coldkey_address == settings.miner_coldkey, (
        f"Coldkey mismatch: got {coldkey_address}, expected {settings.miner_coldkey}"
    )

    return {"coldkey": coldkey_address}


def step_04_bittensor_chain(*, dry_run: bool) -> dict:
    """Connect to subtensor, resolve miner UID/slot, get current block."""
    try:
        from app.transfer import get_subtensor, resolve_slot
    except ImportError:
        raise NotImplementedError("app.transfer module not yet created")

    try:
        from app.config import settings
    except ImportError:
        raise NotImplementedError("app.config module not yet created")

    if dry_run:
        log("DRY-RUN: Connecting to BT chain (read-only)...", indent=1)

    subtensor = get_subtensor(network=settings.bt_network)
    current_block = subtensor.get_current_block()
    log(f"Network: {settings.bt_network}", indent=1)
    log(f"Current block: {current_block}", indent=1)
    assert current_block > 0, "Block height must be > 0"

    slot = resolve_slot(settings.miner_hotkey, network=settings.bt_network)
    log(f"Miner UID/slot: {slot}", indent=1)
    assert slot >= 0, "Slot must be >= 0"

    return {"block": current_block, "slot": slot}


def step_05_verifier_api(*, dry_run: bool) -> dict:
    """Call /v1/miner/status, fetch all positions."""
    try:
        from app.verifier_client import fetch_miner_positions
    except ImportError:
        raise NotImplementedError("app.verifier_client module not yet created")

    try:
        from app.config import settings
    except ImportError:
        raise NotImplementedError("app.config module not yet created")

    # We need a slot - try to resolve from chain
    try:
        from app.transfer import resolve_slot
        slot = resolve_slot(settings.miner_hotkey, network=settings.bt_network)
        log(f"Resolved slot: {slot}", indent=1)
    except Exception as e:
        log(f"WARNING: Could not resolve slot ({e}), using slot=0", indent=1)
        slot = 0

    positions = fetch_miner_positions(hotkey=settings.miner_hotkey, slot=slot)
    log(f"Total positions returned: {len(positions)}", indent=1)

    for i, pos in enumerate(positions[:5]):
        log(f"  Position {i}: evm={pos.evm_address[:16]}... pool={pos.pool_id[:16]}... "
            f"amount={pos.scoring_amount:.2f} lock_days={pos.lock_days} "
            f"active={pos.is_active} expired={pos.is_expired()}", indent=1)
    if len(positions) > 5:
        log(f"  ... and {len(positions) - 5} more", indent=1)

    return {"position_count": len(positions)}


def step_06_position_filtering(*, dry_run: bool) -> dict:
    """Filter positions: is_active=True AND not expired."""
    try:
        from app.verifier_client import fetch_miner_positions, fetch_scoring_positions
    except ImportError:
        raise NotImplementedError("app.verifier_client module not yet created")

    try:
        from app.config import settings
    except ImportError:
        raise NotImplementedError("app.config module not yet created")

    try:
        from app.transfer import resolve_slot
        slot = resolve_slot(settings.miner_hotkey, network=settings.bt_network)
    except Exception:
        slot = 0

    all_positions = fetch_miner_positions(hotkey=settings.miner_hotkey, slot=slot)
    scoring_positions = fetch_scoring_positions(hotkey=settings.miner_hotkey, slot=slot)

    skipped = len(all_positions) - len(scoring_positions)
    log(f"Total positions: {len(all_positions)}", indent=1)
    log(f"Scoring (active + not expired): {len(scoring_positions)}", indent=1)
    log(f"Skipped: {skipped}", indent=1)

    # Log skip reasons
    for pos in all_positions:
        if not pos.is_active:
            log(f"  SKIP: {pos.evm_address[:16]}... reason=not_active", indent=1)
        elif pos.is_expired():
            log(f"  SKIP: {pos.evm_address[:16]}... reason=expired (expires_at={pos.expires_at})", indent=1)

    return {"total": len(all_positions), "scoring": len(scoring_positions), "skipped": skipped}


def step_07_scoring(*, dry_run: bool) -> dict:
    """Score filtered positions, group by EVM address, calculate shares."""
    try:
        from app.scoring import score_positions_segmented
    except ImportError:
        raise NotImplementedError("app.scoring module not yet created")

    try:
        from app.verifier_client import fetch_scoring_positions
        from app.config import settings
        from app.transfer import resolve_slot
    except ImportError:
        raise NotImplementedError("app.verifier_client or app.config not yet created")

    try:
        slot = resolve_slot(settings.miner_hotkey, network=settings.bt_network)
    except Exception:
        slot = 0

    positions = fetch_scoring_positions(hotkey=settings.miner_hotkey, slot=slot)
    if not positions:
        log("No active positions to score (graceful empty handling)", indent=1)
        return {"positions": 0, "addresses": 0}

    # Test with a simulated 1.0 ALPHA total for share validation
    test_alpha = 1.0
    segmented = score_positions_segmented(
        positions=positions,
        total_alpha=test_alpha,
        home_evm_addresses=settings.home_evm_addresses,
        commission_rate=settings.commission_rate,
    )

    log(f"Total score: {segmented.total_score:.4f}", indent=1)
    log(f"Home share: {segmented.home_share:.4%}", indent=1)
    log(f"Guest share: {segmented.guest_share:.4%}", indent=1)
    log(f"Commission ALPHA: {segmented.commission_alpha:.6f}", indent=1)
    log(f"Home positions: {len(segmented.home_positions)}", indent=1)
    log(f"Guest positions: {len(segmented.guest_positions)}", indent=1)

    # Verify shares sum to ~1.0
    total_share = segmented.home_share + segmented.guest_share
    log(f"Share sum: {total_share:.6f} (expected ~1.0)", indent=1)
    assert abs(total_share - 1.0) < 0.0001, f"Shares don't sum to 1.0: {total_share}"

    # Verify home addresses have 0 commission
    for sp in segmented.home_positions:
        log(f"  HOME: {sp.evm_address[:16]}... score={sp.raw_score:.4f} "
            f"share={sp.share:.4%} reward={sp.reward_alpha:.6f}", indent=1)

    for sp in segmented.guest_positions[:5]:
        log(f"  GUEST: {sp.evm_address[:16]}... score={sp.raw_score:.4f} "
            f"share={sp.share:.4%} reward={sp.reward_alpha:.6f}", indent=1)

    unique_addresses = set()
    for sp in segmented.home_positions + segmented.guest_positions:
        unique_addresses.add(sp.evm_address.lower())

    return {
        "positions": len(positions),
        "unique_addresses": len(unique_addresses),
        "home_count": len(segmented.home_positions),
        "guest_count": len(segmented.guest_positions),
    }


def step_08_sweep_to_aggregator(*, dry_run: bool) -> dict:
    """Move all stake from miner hotkey to aggregator hotkey."""
    try:
        from app.transfer import sweep_to_aggregator, get_stake_balance
    except ImportError:
        raise NotImplementedError("app.transfer module not yet created")

    try:
        from app.config import settings
    except ImportError:
        raise NotImplementedError("app.config module not yet created")

    if dry_run:
        log("DRY-RUN: Simulating sweep to aggregator", indent=1)
        log("  Simulated stake balance: 0.5 ALPHA", indent=1)
        log("  Simulated sweep amount: 0.5 ALPHA", indent=1)
        log("  Simulated success: True", indent=1)
        return {"dry_run": True, "simulated_amount": 0.5}

    # Live mode
    stake_before = get_stake_balance(
        hotkey=settings.miner_hotkey,
        coldkey=settings.miner_coldkey,
        network=settings.bt_network,
    )
    log(f"Stake before sweep: {stake_before:.9f} ALPHA", indent=1)

    success, amount = sweep_to_aggregator(
        miner_hotkey=settings.miner_hotkey,
        aggregator_hotkey=settings.aggregator_hotkey,
        network=settings.bt_network,
    )
    log(f"Sweep success: {success}", indent=1)
    log(f"Amount swept: {amount:.9f} ALPHA", indent=1)

    return {"success": success, "amount": amount, "stake_before": stake_before}


def step_09_reward_recording(*, dry_run: bool) -> dict:
    """Test reward recording logic. DRY-RUN: simulate only, no DB writes."""
    try:
        from app.jobs.sweep_and_score import record_sweep_and_rewards
        from app.database import dispose_engine
    except ImportError:
        raise NotImplementedError("app.jobs.sweep_and_score module not yet created")

    if dry_run:
        # DRY-RUN: verify the scoring pipeline works WITHOUT writing to DB
        try:
            from app.scoring import score_positions_segmented
            from app.verifier_client import fetch_scoring_positions
            from app.transfer import resolve_slot
            from app.config import settings
        except ImportError as e:
            raise NotImplementedError(f"Missing module: {e}")

        log("DRY-RUN: Testing scoring pipeline (no DB writes)", indent=1)

        slot = resolve_slot(settings.miner_hotkey, network=settings.bt_network)
        log(f"Resolved miner slot: {slot}", indent=1)

        positions = fetch_scoring_positions(
            hotkey=settings.miner_hotkey, slot=slot
        )
        log(f"Active positions: {len(positions)}", indent=1)

        if not positions:
            log("No active positions found - scoring would produce no entries", indent=1)
            return {"dry_run": True, "positions": 0, "entries": 0}

        test_alpha = 0.5  # Simulated amount for score calculation
        segmented = score_positions_segmented(
            positions=positions,
            total_alpha=test_alpha,
            home_evm_addresses=settings.home_evm_addresses,
            commission_rate=settings.commission_rate,
        )

        log(f"Home positions: {len(segmented.home_positions)}", indent=1)
        log(f"Guest positions: {len(segmented.guest_positions)}", indent=1)
        log(f"Total commission (simulated): {segmented.commission_alpha:.6f}", indent=1)
        log(f"Total score: {segmented.total_score:.6f}", indent=1)

        # Show per-address breakdown (simulated, not written to DB)
        all_scored = segmented.home_positions + segmented.guest_positions
        addrs = set()
        for sp in all_scored:
            addr = sp.evm_address.lower()
            if addr not in addrs:
                addrs.add(addr)
                log(f"  {addr[:16]}... score={sp.raw_score:.4f} share={sp.share:.4f}", indent=1)

        log(f"Would create {len(addrs)} reward entries (NOT written to DB)", indent=1)
        return {
            "dry_run": True,
            "positions": len(positions),
            "unique_addresses": len(addrs),
            "total_commission": segmented.commission_alpha,
        }

    # LIVE mode: actually write to DB
    test_block = 999999999
    test_alpha = 0.5

    async def run_test():
        result = await record_sweep_and_rewards(
            bt_epoch_block=test_block,
            alpha_amount=test_alpha,
        )

        log(f"Sweep ID: {result['sweep_id']}", indent=1)
        log(f"Reward entries created: {result['entries_created']}", indent=1)
        log(f"Total commission: {result['total_commission']:.6f}", indent=1)

        for addr, net in result.get("per_address", {}).items():
            log(f"  {addr[:16]}... -> {net:.6f} ALPHA", indent=1)

        # Test idempotency: re-insert should be no-op
        result2 = await record_sweep_and_rewards(
            bt_epoch_block=test_block,
            alpha_amount=test_alpha,
        )
        log(f"Idempotent re-insert: entries_created={result2['entries_created']} (expected 0)", indent=1)
        assert result2["entries_created"] == 0, "Idempotent re-insert should create 0 entries"

        await dispose_engine()
        return result

    return asyncio.run(run_test())


def step_10_reward_balance_query(*, dry_run: bool) -> dict:
    """Compute available balance for each EVM address from DB."""
    try:
        from app.database import compute_balances
    except ImportError:
        raise NotImplementedError("app.database compute_balances not yet created")

    balances = asyncio.run(compute_balances())

    log(f"Addresses with balances: {len(balances)}", indent=1)
    for addr, bal in list(balances.items())[:10]:
        log(f"  {addr[:16]}... earned={bal['total_earned']:.6f} "
            f"claimed={bal['total_claimed']:.6f} available={bal['available']:.6f}", indent=1)
    if len(balances) > 10:
        log(f"  ... and {len(balances) - 10} more", indent=1)

    # Verify no negative balances
    for addr, bal in balances.items():
        assert bal["available"] >= 0, f"Negative balance for {addr}: {bal['available']}"

    return {"address_count": len(balances)}


def step_11_api_smoke_test(*, dry_run: bool) -> dict:
    """Hit all API endpoints using FastAPI TestClient."""
    try:
        from app.main import app
    except ImportError:
        raise NotImplementedError("app.main module not yet created")

    from fastapi.testclient import TestClient

    results = {}

    with TestClient(app) as client:
        # GET /health
        r = client.get("/health")
        log(f"GET /health -> {r.status_code}: {r.json()}", indent=1)
        assert r.status_code == 200
        results["health"] = r.status_code

        # GET /api/miner-info
        r = client.get("/api/miner-info")
        log(f"GET /api/miner-info -> {r.status_code}", indent=1)
        assert r.status_code == 200
        results["miner_info"] = r.status_code

        # GET /api/rewards/{addr}
        test_addr = "0x0000000000000000000000000000000000000001"
        r = client.get(f"/api/rewards/{test_addr}")
        log(f"GET /api/rewards/{{addr}} -> {r.status_code}: {r.json()}", indent=1)
        assert r.status_code == 200
        results["rewards"] = r.status_code

        # GET /api/claims/{addr}
        r = client.get(f"/api/claims/{test_addr}")
        log(f"GET /api/claims/{{addr}} -> {r.status_code}: {r.json()}", indent=1)
        assert r.status_code == 200
        results["claims"] = r.status_code

    return results


def step_12_evm_signature(*, dry_run: bool) -> dict:
    """Generate a test EVM keypair, sign a claim message, verify recovery."""
    try:
        from app.auth import verify_claim_signature
    except ImportError:
        raise NotImplementedError("app.auth module not yet created")

    from eth_account import Account
    from eth_account.messages import encode_defunct

    # Generate test keypair
    acct = Account.create()
    test_address = acct.address.lower()
    log(f"Test EVM address: {test_address}", indent=1)

    # Build claim message
    bt_coldkey = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
    amount = 1.5
    timestamp = int(time.time())
    message = f"Claim {amount} ALPHA to {bt_coldkey} | {timestamp}"
    log(f"Message: {message}", indent=1)

    # Sign with test private key
    msg = encode_defunct(text=message)
    signed = acct.sign_message(msg)
    signature = signed.signature.hex()
    log(f"Signature: {signature[:32]}...", indent=1)

    # Verify recovery
    recovered = verify_claim_signature(
        evm_address=test_address,
        signature=signature,
        message=message,
    )
    log(f"Recovered address: {recovered}", indent=1)
    log(f"Match: {'YES' if recovered.lower() == test_address else 'NO'}", indent=1)

    assert recovered.lower() == test_address, (
        f"Signature recovery mismatch: got {recovered}, expected {test_address}"
    )

    return {"address": test_address, "recovered": recovered}


def step_13_claim_submission(*, dry_run: bool) -> dict:
    """POST /api/claim with valid signature, verify rejection on insufficient balance."""
    try:
        from app.main import app
    except ImportError:
        raise NotImplementedError("app.main module not yet created")

    try:
        from app.auth import verify_claim_signature
    except ImportError:
        raise NotImplementedError("app.auth module not yet created")

    from fastapi.testclient import TestClient
    from eth_account import Account
    from eth_account.messages import encode_defunct

    # Generate test claim
    acct = Account.create()
    test_address = acct.address.lower()
    bt_coldkey = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
    amount = 0.001
    timestamp = int(time.time())
    message = f"Claim {amount} ALPHA to {bt_coldkey} | {timestamp}"

    msg = encode_defunct(text=message)
    signed = acct.sign_message(msg)
    signature = signed.signature.hex()

    with TestClient(app) as client:
        # Check balance before
        r = client.get(f"/api/rewards/{test_address}")
        balance_before = r.json().get("available", 0)
        log(f"Balance before claim: {balance_before}", indent=1)

        # Submit claim
        r = client.post("/api/claim", json={
            "evm_address": test_address,
            "bt_coldkey": bt_coldkey,
            "amount_alpha": amount,
            "signature": signature,
            "message": message,
            "timestamp": timestamp,
        })
        log(f"POST /api/claim -> {r.status_code}: {r.json()}", indent=1)

        # With no balance, expect insufficient balance rejection
        if balance_before < amount:
            log(f"Expected: insufficient balance (have {balance_before}, need {amount})", indent=1)
            assert r.status_code in (400, 422), f"Expected 400/422, got {r.status_code}"
            return {"status": "insufficient_balance_correctly_rejected"}

        assert r.status_code == 200
        claim_data = r.json()
        log(f"Claim ID: {claim_data.get('claim_id')}", indent=1)
        log(f"Status: {claim_data.get('status')}", indent=1)

        # Check balance after
        r = client.get(f"/api/rewards/{test_address}")
        balance_after = r.json().get("available", 0)
        log(f"Balance after claim: {balance_after}", indent=1)

        return {
            "claim_id": claim_data.get("claim_id"),
            "balance_before": balance_before,
            "balance_after": balance_after,
        }


def step_14_claim_transfer(*, dry_run: bool) -> dict:
    """Execute actual BT transfer from aggregator to test coldkey."""
    if dry_run:
        log("DRY-RUN: Skipping live BT transfer", indent=1)
        raise NotImplementedError("Step 14 only runs in --live mode")

    try:
        from app.transfer import transfer_claim
    except ImportError:
        raise NotImplementedError("app.transfer module not yet created")

    try:
        from app.config import settings
    except ImportError:
        raise NotImplementedError("app.config module not yet created")

    # Use a test coldkey and tiny amount
    test_coldkey = settings.miner_coldkey  # Transfer to self for testing
    test_amount = 0.000001

    log(f"Transferring {test_amount} ALPHA to {test_coldkey[:16]}...", indent=1)
    tx_hash = transfer_claim(
        recipient_coldkey=test_coldkey,
        alpha_amount=test_amount,
        aggregator_hotkey=settings.aggregator_hotkey,
        network=settings.bt_network,
    )
    log(f"TX hash: {tx_hash}", indent=1)

    return {"tx_hash": tx_hash, "amount": test_amount}


def step_15_slack_notification(*, dry_run: bool) -> dict:
    """Send test notification to Slack webhook."""
    try:
        from app.slack_notifier import SlackNotifier
    except ImportError:
        raise NotImplementedError("app.slack_notifier module not yet created")

    try:
        from app.config import settings
    except ImportError:
        raise NotImplementedError("app.config module not yet created")

    if not settings.slack_webhook_url:
        log("SKIP: No SLACK_WEBHOOK_URL configured", indent=1)
        raise NotImplementedError("SLACK_WEBHOOK_URL not configured")

    notifier = SlackNotifier(
        webhook_url=settings.slack_webhook_url,
        error_webhook_url=settings.slack_error_webhook_url,
    )

    success = notifier.send_message(
        "[E2E TEST] Principal Rewards system test notification. Ignore this.",
        level="info",
    )
    log(f"Slack response: {'200 OK' if success else 'FAILED'}", indent=1)
    assert success, "Slack notification failed"

    return {"sent": True}


def step_16_blackout_window(*, dry_run: bool) -> dict:
    """Simulate times inside/outside the Thu 23:30 - Fri 00:30 window."""
    try:
        from app.jobs.epoch_monitor import is_in_blackout_window
    except ImportError:
        raise NotImplementedError("app.jobs.epoch_monitor module not yet created")

    from datetime import datetime, timezone

    test_cases = [
        # (description, weekday, hour, minute, expected_blackout)
        ("Wednesday 14:00 UTC", 2, 14, 0, False),
        ("Thursday 23:00 UTC", 3, 23, 0, False),
        ("Thursday 23:30 UTC", 3, 23, 30, True),    # Blackout starts
        ("Thursday 23:45 UTC", 3, 23, 45, True),
        ("Friday 00:00 UTC", 4, 0, 0, True),         # Cartha epoch boundary
        ("Friday 00:15 UTC", 4, 0, 15, True),
        ("Friday 00:30 UTC", 4, 0, 30, False),       # Blackout ends
        ("Friday 01:00 UTC", 4, 1, 0, False),
        ("Saturday 12:00 UTC", 5, 12, 0, False),
    ]

    all_passed = True
    for desc, weekday, hour, minute, expected in test_cases:
        # Create a datetime with the given weekday
        # 2026-02-09 is a Monday (weekday=0), so we offset
        from datetime import timedelta
        base_date = datetime(2026, 2, 9, tzinfo=timezone.utc)  # Monday
        test_dt = base_date + timedelta(days=weekday)
        test_dt = test_dt.replace(hour=hour, minute=minute, second=0)

        result = is_in_blackout_window(test_dt)
        status = "✓" if result == expected else "✗"
        log(f"  {status} {desc}: blackout={result} (expected={expected})", indent=1)
        if result != expected:
            all_passed = False

    assert all_passed, "Some blackout window tests failed"
    return {"test_cases": len(test_cases), "all_passed": all_passed}


def step_17_epoch_monitor_cycle(*, dry_run: bool) -> dict:
    """Run one full epoch monitor iteration."""
    try:
        from app.jobs.epoch_monitor import EpochMonitor
    except ImportError:
        raise NotImplementedError("app.jobs.epoch_monitor module not yet created")

    try:
        from app.config import settings
    except ImportError:
        raise NotImplementedError("app.config module not yet created")

    monitor = EpochMonitor(
        miner_hotkey=settings.miner_hotkey,
        network=settings.bt_network,
        poll_interval=settings.poll_interval,
        dry_run=dry_run,
    )

    log("Running one epoch monitor poll cycle...", indent=1)
    result = monitor.poll_once()

    log(f"Current block: {result.get('current_block')}", indent=1)
    log(f"Next epoch block: {result.get('next_epoch_block')}", indent=1)
    log(f"In blackout: {result.get('in_blackout')}", indent=1)
    log(f"Triggered: {result.get('triggered')}", indent=1)

    return result


# ─── Step Registry ────────────────────────────────────────────────────────────

STEPS = [
    (1, "Config Loading", step_01_config_loading),
    (2, "Database Connection", step_02_database_connection),
    (3, "GCP Secret Manager Wallet Unlock", step_03_gcp_wallet_unlock),
    (4, "Bittensor Chain Connection", step_04_bittensor_chain),
    (5, "Verifier API Connection", step_05_verifier_api),
    (6, "Position Filtering", step_06_position_filtering),
    (7, "Scoring Calculation", step_07_scoring),
    (8, "Sweep to Aggregator", step_08_sweep_to_aggregator),
    (9, "Reward Recording", step_09_reward_recording),
    (10, "Reward Balance Query", step_10_reward_balance_query),
    (11, "API Server Smoke Test", step_11_api_smoke_test),
    (12, "EVM Signature Verification", step_12_evm_signature),
    (13, "Claim Submission", step_13_claim_submission),
    (14, "Claim Transfer (live only)", step_14_claim_transfer),
    (15, "Slack Notification", step_15_slack_notification),
    (16, "Blackout Window Logic", step_16_blackout_window),
    (17, "Epoch Monitor Cycle", step_17_epoch_monitor_cycle),
]


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E Test for Internal Principal Rewards")
    parser.add_argument("--live", action="store_true", help="Run with real BT chain operations")
    parser.add_argument("--step", type=int, help="Run only this step number")
    parser.add_argument("--from-step", type=int, default=1, help="Start from this step number")
    args = parser.parse_args()

    dry_run = not args.live
    mode = "LIVE" if args.live else "DRY-RUN"

    print()
    print(f"[{ts()}] {'=' * 60}")
    print(f"[{ts()}] INTERNAL PRINCIPAL REWARDS - E2E TEST")
    print(f"[{ts()}] Mode: {mode}")
    print(f"[{ts()}] Project: {PROJECT_ROOT}")
    print(f"[{ts()}] {'=' * 60}")

    results: list[StepResult] = []

    for step_num, step_name, step_fn in STEPS:
        # Filter by --step or --from-step
        if args.step and step_num != args.step:
            continue
        if step_num < args.from_step:
            continue

        result = run_step(step_num, step_name, step_fn, dry_run=dry_run)
        results.append(result)

    # ─── Summary ──────────────────────────────────────────────────────────

    passed = sum(1 for r in results if r.status == StepStatus.PASSED)
    failed = sum(1 for r in results if r.status == StepStatus.FAILED)
    skipped = sum(1 for r in results if r.status == StepStatus.SKIPPED)
    total = len(results)
    total_time = sum(r.duration_s for r in results)

    print()
    print(f"[{ts()}] {'═' * 60}")
    print(f"[{ts()}] SUMMARY: {passed}/{total} passed, {failed} failed, {skipped} skipped")
    print(f"[{ts()}] Total time: {total_time:.1f}s")
    print(f"[{ts()}] {'═' * 60}")

    # Detail table
    for r in results:
        symbol = {"PASSED": "✓", "FAILED": "✗", "SKIPPED": "⊘"}[r.status.value]
        print(f"[{ts()}]   {symbol} Step {r.step:2d}: {r.name} ({r.duration_s:.1f}s)")
        if r.error and r.status == StepStatus.FAILED:
            print(f"[{ts()}]          Error: {r.error}")

    print(f"[{ts()}] {'═' * 60}")

    if failed > 0:
        sys.exit(1)
    elif passed == 0:
        print(f"[{ts()}] All steps skipped. Start building components!")
        sys.exit(0)
    else:
        print(f"[{ts()}] All executed steps passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()

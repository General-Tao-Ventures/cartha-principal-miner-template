"""BT epoch monitor with Cartha weekly epoch blackout.

Polls the Bittensor chain for epoch boundaries (every 360 blocks / ~72 min).
When triggered, runs sweep + score. Respects the weekly blackout window
around the Cartha epoch freeze (Thu 23:30 - Fri 00:30 UTC).

Can be run as a standalone process via: python -m app.jobs.epoch_monitor
"""

from __future__ import annotations

import asyncio
import gc
import logging
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

MAX_RETRIES = 3
RETRY_DELAY = 60  # seconds between retries (BT chain calls can take 30s+)
SUBTENSOR_TIMEOUT = 120  # seconds before considering subtensor call hung
SWEEP_TIMEOUT = 180  # seconds for sweep operation
SCORING_TIMEOUT = 180  # seconds for scoring operation
SUBTENSOR_RECONNECT_INTERVAL = 300  # recreate subtensor every 5 min to avoid stale conn


# ─── Blackout Window ──────────────────────────────────────────────────────────


def is_in_blackout_window(dt: datetime | None = None) -> bool:
    """Check if the given time is in the Cartha weekly epoch blackout.

    Blackout: Thursday 23:30 UTC to Friday 00:30 UTC
    This is when the Cartha verifier freezes new position lists.

    Args:
        dt: Datetime to check (default: now UTC)

    Returns:
        True if in blackout window
    """
    if dt is None:
        dt = datetime.now(timezone.utc)

    weekday = dt.weekday()  # 0=Mon, 3=Thu, 4=Fri
    hour = dt.hour
    minute = dt.minute
    time_mins = hour * 60 + minute

    # Thursday 23:30 (weekday=3, 23:30 = 1410 mins)
    if weekday == 3 and time_mins >= 1410:
        return True

    # Friday 00:00-00:29 (weekday=4, 0:00-0:29 = 0-29 mins)
    if weekday == 4 and time_mins < 30:
        return True

    return False


# ─── Epoch Monitor ────────────────────────────────────────────────────────────


class EpochMonitor:
    """Monitors BT chain for epoch boundaries and triggers sweep+score.

    Ported from liquidity_flow_controller/neuron/miner.py monitor_epochs().
    """

    def __init__(
        self,
        miner_hotkey: str,
        network: str = "finney",
        poll_interval: float = 30.0,
        dry_run: bool = False,
    ) -> None:
        self.miner_hotkey = miner_hotkey
        self.network = network
        self.poll_interval = poll_interval
        self.dry_run = dry_run

        self._stop = False
        self._last_epoch_index: int | None = None
        self._next_epoch_block: int | None = None
        self._tempo: int | None = None

        # Reusable subtensor connection
        self._subtensor = None
        self._subtensor_created_at: float = 0

    def _get_subtensor(self):
        """Get or create a subtensor connection, reconnecting if stale."""
        now = time.time()
        if (
            self._subtensor is None
            or (now - self._subtensor_created_at) > SUBTENSOR_RECONNECT_INTERVAL
        ):
            self._close_subtensor()
            try:
                from ..transfer import get_subtensor
                self._subtensor = get_subtensor(network=self.network)
                self._subtensor_created_at = now
                logger.debug("Created new subtensor connection")
            except Exception as e:
                logger.error(f"Failed to create subtensor: {e}")
                self._subtensor = None
                raise
        return self._subtensor

    def _close_subtensor(self):
        """Close existing subtensor connection to free memory."""
        if self._subtensor is not None:
            try:
                if hasattr(self._subtensor, "close"):
                    self._subtensor.close()
            except Exception:
                pass
            self._subtensor = None
            gc.collect()

    def poll_once(self) -> dict[str, Any]:
        """Run a single poll cycle. Returns status dict."""
        from ..config import SUBNET_NETUID

        result: dict[str, Any] = {
            "current_block": None,
            "next_epoch_block": self._next_epoch_block,
            "in_blackout": is_in_blackout_window(),
            "triggered": False,
        }

        # Check blackout first (no chain call needed)
        if result["in_blackout"]:
            logger.info("In Cartha weekly blackout window, skipping")
            return result

        # Get current block with timeout
        try:
            subtensor = self._get_subtensor()
            current_block = subtensor.get_current_block()
            result["current_block"] = current_block
        except Exception as e:
            logger.warning(f"Failed to get current block: {e}")
            self._close_subtensor()  # Force reconnect on next poll
            result["error"] = str(e)
            return result

        # Detect epoch boundary
        triggered = False

        # Method 1: get_next_epoch_start_block
        if self._next_epoch_block is None:
            try:
                self._next_epoch_block = subtensor.get_next_epoch_start_block(
                    SUBNET_NETUID
                )
                result["next_epoch_block"] = self._next_epoch_block
            except Exception as e:
                logger.debug(f"Could not get next epoch block: {e}")

        if self._next_epoch_block is not None and current_block >= self._next_epoch_block:
            triggered = True
        # Method 2: Fallback tempo-based
        elif self._next_epoch_block is None:
            if self._tempo is None:
                try:
                    self._tempo = subtensor.tempo(SUBNET_NETUID)
                except Exception:
                    self._tempo = 360  # Default BT tempo

            if self._tempo:
                epoch_index = current_block // self._tempo
                if self._last_epoch_index is None:
                    self._last_epoch_index = epoch_index
                elif epoch_index > self._last_epoch_index:
                    triggered = True
                    self._last_epoch_index = epoch_index

        result["triggered"] = triggered

        if triggered:
            logger.info(
                f">>> EPOCH BOUNDARY at block {current_block} "
                f"(next was {self._next_epoch_block}), triggering sweep+score"
            )
            self._process_epoch_with_retry(current_block)

            # Update next epoch block
            try:
                self._next_epoch_block = subtensor.get_next_epoch_start_block(
                    SUBNET_NETUID, block=current_block
                )
                result["next_epoch_block"] = self._next_epoch_block
                logger.info(f"Next epoch at block {self._next_epoch_block}")
            except Exception:
                self._next_epoch_block = None
        else:
            # Periodic status log (every ~5 min / 10 polls)
            blocks_left = (
                self._next_epoch_block - current_block
                if self._next_epoch_block
                else "?"
            )
            logger.debug(
                f"Block {current_block}, next epoch in ~{blocks_left} blocks"
            )

        return result

    def _process_epoch_with_retry(self, block: int) -> None:
        """Attempt sweep+score with retries."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._log(f"Processing epoch at block {block} (attempt {attempt}/{MAX_RETRIES})...")
                self._process_epoch(block)
                self._log(f"Epoch processing SUCCEEDED on attempt {attempt}")
                return  # Success
            except Exception as e:
                import traceback
                err_tb = traceback.format_exc()
                self._log(f"Epoch attempt {attempt}/{MAX_RETRIES} FAILED: {e}")
                print(f"[EPOCH] Traceback:\n{err_tb}", flush=True)
                if attempt < MAX_RETRIES:
                    self._log(f"Retrying in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                    self._close_subtensor()
                else:
                    self._log(f"All {MAX_RETRIES} attempts FAILED for block {block}")
                    self._notify_error(block, e)

    def _log(self, msg: str) -> None:
        """Log to both logger AND print (bittensor suppresses logger)."""
        logger.info(msg)
        print(f"[EPOCH] {msg}", flush=True)

    def _process_epoch(self, block: int) -> None:
        """Sweep + score for this epoch.

        Runs the sweep inside an async context so we can acquire the same
        global PostgreSQL advisory lock (key=0) used by the claim endpoint.
        This prevents nonce conflicts from concurrent chain transactions.
        """
        if self.dry_run:
            self._log(f"DRY-RUN: Would sweep+score at block {block}")
            return

        from ..wallet import load_wallet
        from ..database import reset_engine

        # ── 1. Load wallet (sync, no lock needed) ─────────────────────────
        self._log(f"[1/3] Loading wallet for block {block}...")
        wallet = load_wallet()
        self._log(f"[1/3] Wallet loaded: {wallet.name}")

        # ── 2 & 3. Sweep + score inside async context with global lock ───
        reset_engine()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                self._sweep_and_score_locked(block, wallet)
            )
        finally:
            loop.close()
            reset_engine()

        if result is None:
            return  # Nothing to score (0 amount or sweep failed)

        self._log(
            f"[3/3] SCORING COMPLETE: sweep_id={result['sweep_id']}, "
            f"entries={result['entries_created']}, "
            f"commission={result['total_commission']:.4f} ALPHA"
        )

        # Log per-address breakdown
        for addr, net in result.get("per_address", {}).items():
            self._log(f"  {addr[:10]}... -> {net:.2f} ALPHA")

        # ── Slack notification ────────────────────────────────────────────
        self._notify_success(block, result.get("_alpha_amount", 0.0), result)

    async def _sweep_and_score_locked(
        self, block: int, wallet: Any
    ) -> dict[str, Any] | None:
        """Acquire global chain-transfer lock, sweep, then score.

        Uses the same advisory lock key (0) as the claim endpoint so that
        sweeps and claims never hit the chain simultaneously.
        """
        from ..transfer import sweep_to_aggregator
        from .sweep_and_score import record_sweep_and_rewards
        from ..database import get_session_factory

        from sqlalchemy import text as sa_text

        GLOBAL_CHAIN_LOCK_KEY = 0

        # ── Acquire global lock & sweep in thread pool ────────────────────
        async with get_session_factory()() as lock_session:
            self._log("[2/3] Acquiring global chain-transfer lock for sweep...")
            await lock_session.execute(
                sa_text("SELECT pg_advisory_lock(:key)"),
                {"key": GLOBAL_CHAIN_LOCK_KEY},
            )
            try:
                self._log("[2/3] Sweeping miner hotkey -> aggregator...")
                success, amount = await asyncio.to_thread(
                    sweep_to_aggregator, wallet=wallet
                )
                self._log(
                    f"[2/3] Sweep result: success={success}, "
                    f"amount={amount:.4f} ALPHA"
                )
            finally:
                await lock_session.execute(
                    sa_text("SELECT pg_advisory_unlock(:key)"),
                    {"key": GLOBAL_CHAIN_LOCK_KEY},
                )

        if not success:
            raise RuntimeError("Sweep failed")

        if amount <= 0:
            self._log("[2/3] Sweep amount is 0, nothing to score. Done.")
            return None

        # ── Score and record (no chain lock needed, DB only) ──────────────
        self._log(f"[3/3] Recording sweep and scoring {amount:.4f} ALPHA...")
        result = await record_sweep_and_rewards(
            bt_epoch_block=block,
            alpha_amount=amount,
        )
        result["_alpha_amount"] = amount  # Pass back for Slack notification
        return result

    def _notify_success(self, block: int, amount: float, result: dict) -> None:
        """Send Slack notification for successful sweep."""
        try:
            from ..slack_notifier import SlackNotifier
            from ..config import settings
            if settings.slack_webhook_url:
                notifier = SlackNotifier(
                    webhook_url=settings.slack_webhook_url,
                    error_webhook_url=settings.slack_error_webhook_url,
                )
                notifier.notify_sweep(
                    alpha_amount=amount,
                    miners_scored=result["entries_created"],
                    commission=result["total_commission"],
                    bt_epoch_block=block,
                )
        except Exception as slack_err:
            self._log(f"Slack notification failed: {slack_err}")

    def _notify_error(self, block: int, error: Exception) -> None:
        """Send Slack error notification."""
        try:
            from ..slack_notifier import SlackNotifier
            from ..config import settings
            if settings.slack_error_webhook_url:
                notifier = SlackNotifier(
                    webhook_url=settings.slack_webhook_url,
                    error_webhook_url=settings.slack_error_webhook_url,
                )
                notifier.send_error(
                    f"*Epoch Processing FAILED* (block `{block}`)\n"
                    f"Error: `{error}`\n"
                    f"All {MAX_RETRIES} retry attempts exhausted."
                )
        except Exception:
            pass

    def run(self) -> None:
        """Run the epoch monitor loop until stopped."""
        logger.info(
            f"Starting epoch monitor: hotkey={self.miner_hotkey[:16]}..., "
            f"network={self.network}, poll={self.poll_interval}s, "
            f"dry_run={self.dry_run}"
        )

        # Sweep on startup -- catch any accumulated earnings from downtime
        try:
            self._log("Startup sweep: connecting to chain...")
            subtensor = self._get_subtensor()
            current_block = subtensor.get_current_block()
            self._log(f"Startup sweep: block={current_block}, sweeping accumulated earnings...")
            self._process_epoch_with_retry(current_block)
        except Exception as e:
            self._log(f"Startup sweep failed (will retry on next epoch): {e}")

        poll_count = 0
        while not self._stop:
            try:
                result = self.poll_once()
                poll_count += 1

                # Log status every 10 polls (~5 min at 30s interval)
                if poll_count % 10 == 0:
                    logger.info(
                        f"Poll #{poll_count}: block={result.get('current_block')}, "
                        f"next_epoch={result.get('next_epoch_block')}, "
                        f"blackout={result.get('in_blackout')}"
                    )

            except Exception as e:
                logger.error(f"Poll error: {e}", exc_info=True)
                self._close_subtensor()  # Force reconnect

            time.sleep(self.poll_interval)

        # Cleanup on exit
        self._close_subtensor()
        logger.info("Epoch monitor stopped.")

    def stop(self) -> None:
        """Signal the monitor to stop."""
        self._stop = True


# ─── Entrypoint ───────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    """Set up logging that survives bittensor's root logger override.

    Bittensor calls `bt.logging.set_warning()` on import which sets root
    logger to WARNING, silently killing all our INFO logs. We fix this by
    adding our own handler directly to our logger with explicit INFO level.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    # Attach to our logger directly (not root) so bittensor can't suppress it
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # Don't let root logger filter us


def main() -> None:
    """Run the epoch monitor as a standalone process (for pm2)."""
    _setup_logging()

    from ..config import settings

    logger.info("=" * 60)
    logger.info("PRINCIPAL MINER EPOCH MONITOR STARTING")
    logger.info(f"  Hotkey: {settings.miner_hotkey[:16]}...")
    logger.info(f"  Aggregator: {settings.aggregator_hotkey[:16]}...")
    logger.info(f"  Network: {settings.bt_network}")
    logger.info(f"  Poll interval: {settings.poll_interval}s")
    logger.info("=" * 60)

    monitor = EpochMonitor(
        miner_hotkey=settings.miner_hotkey,
        network=settings.bt_network,
        poll_interval=settings.poll_interval,
    )

    # Handle signals for graceful shutdown
    def handle_signal(signum, frame):
        logger.info(f"Received signal {signum}, stopping...")
        monitor.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    monitor.run()


if __name__ == "__main__":
    main()

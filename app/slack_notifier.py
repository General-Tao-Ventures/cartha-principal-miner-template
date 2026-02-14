"""Slack notification system for the internal principal rewards backend.

Ported pattern from liquidity_flow_controller/utils/slack_notifier.py.
Supports separate info/error channels and daily summaries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)


class SlackNotifier:
    """Handles all Slack notifications for the principal rewards system."""

    # Class-level flag: only ONE daily summary thread across all instances
    _daily_thread_started = False
    _daily_thread_lock = threading.Lock()

    def __init__(
        self,
        webhook_url: str = "",
        error_webhook_url: str = "",
    ) -> None:
        self.webhook_url = webhook_url
        self.error_webhook_url = error_webhook_url or webhook_url
        self.enabled = bool(webhook_url)
        self.hostname = self._get_hostname()

        # Daily metrics (reset at midnight UTC) — kept as fallback
        self.daily_lock = threading.Lock()
        self.daily_metrics: dict[str, Any] = {
            "sweeps_count": 0,
            "sweeps_failed": 0,
            "total_alpha_swept": 0.0,
            "total_commission": 0.0,
            "claims_count": 0,
            "claims_failed": 0,
            "total_alpha_claimed": 0.0,
            "miners_scored": 0,
        }

        if self.enabled:
            self._maybe_start_daily_summary_thread()

    @staticmethod
    def _get_hostname() -> str:
        try:
            return socket.gethostname()
        except Exception:
            return "unknown"

    # ─── Send Message ─────────────────────────────────────────────────────

    def send_message(
        self,
        text: str,
        level: str = "info",
        fields: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Send a message to Slack.

        Args:
            text: Message text
            level: "info" or "error" (determines channel)
            fields: Optional attachment fields

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False

        url = self.error_webhook_url if level == "error" else self.webhook_url

        payload: dict[str, Any] = {"text": text}

        if fields:
            payload["attachments"] = [
                {
                    "color": "#ff0000" if level == "error" else "#36a64f",
                    "fields": fields,
                }
            ]

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=10,
            )
            return response.status_code == 200
        except Exception:
            return False

    # ─── Sweep Notifications ──────────────────────────────────────────────

    def notify_sweep(
        self,
        alpha_amount: float,
        miners_scored: int,
        commission: float,
        bt_epoch_block: int,
    ) -> None:
        """Send notification for a completed sweep + score cycle."""
        with self.daily_lock:
            self.daily_metrics["sweeps_count"] += 1
            self.daily_metrics["total_alpha_swept"] += alpha_amount
            self.daily_metrics["total_commission"] += commission
            self.daily_metrics["miners_scored"] = miners_scored

        net_distributed = alpha_amount - commission
        self.send_message(
            f"*Epoch Sweep Complete* (block `{bt_epoch_block}`)\n"
            f"Swept: *{alpha_amount:.2f} ALPHA*\n"
            f"Distributed: {net_distributed:.2f} ALPHA | Commission: {commission:.2f} ALPHA\n"
            f"Miners scored: {miners_scored}",
            level="info",
        )

    def notify_sweep_failure(self, error: str, bt_epoch_block: int) -> None:
        """Send notification for a failed sweep."""
        with self.daily_lock:
            self.daily_metrics["sweeps_failed"] += 1

        self.send_message(
            f"*Epoch Sweep FAILED* (block `{bt_epoch_block}`)\n"
            f"Error: `{error}`",
            level="error",
        )

    # ─── Claim Notifications ──────────────────────────────────────────────

    def notify_claim(
        self,
        evm_address: str,
        bt_coldkey: str,
        amount: float,
        tx_hash: str | None,
        status: str,
    ) -> None:
        """Send notification for a claim attempt."""
        with self.daily_lock:
            if status == "completed":
                self.daily_metrics["claims_count"] += 1
                self.daily_metrics["total_alpha_claimed"] += amount
            elif status == "failed":
                self.daily_metrics["claims_failed"] += 1

        emoji = "white_check_mark" if status == "completed" else "x"
        level = "info" if status == "completed" else "error"

        tx_display = "N/A"
        if tx_hash:
            tx_display = f"<https://tao.app/extrinsic/{tx_hash}|{tx_hash}>"

        self.send_message(
            f":{emoji}: *Claim {status.upper()}*\n"
            f"Amount: *{amount:.2f} ALPHA*\n"
            f"EVM: `{evm_address[:10]}...{evm_address[-4:]}`\n"
            f"BT Coldkey: `{bt_coldkey[:10]}...{bt_coldkey[-4:]}`\n"
            f"TX: {tx_display}",
            level=level,
        )

    # ─── Daily Summary ────────────────────────────────────────────────────

    def _maybe_start_daily_summary_thread(self) -> None:
        """Start background thread that sends daily summary at midnight UTC.

        Uses a class-level lock so only ONE thread runs across all instances.
        """
        with SlackNotifier._daily_thread_lock:
            if SlackNotifier._daily_thread_started:
                return
            SlackNotifier._daily_thread_started = True

        # Capture webhook URLs for the thread (it outlives any single instance)
        webhook_url = self.webhook_url
        error_webhook_url = self.error_webhook_url

        def loop():
            while True:
                now = datetime.now(timezone.utc)
                next_midnight = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                if next_midnight <= now:
                    next_midnight += timedelta(days=1)

                sleep_secs = (next_midnight - now).total_seconds()
                time.sleep(sleep_secs)

                try:
                    notifier = SlackNotifier.__new__(SlackNotifier)
                    notifier.webhook_url = webhook_url
                    notifier.error_webhook_url = error_webhook_url
                    notifier.enabled = True
                    notifier.hostname = SlackNotifier._get_hostname()
                    notifier._send_daily_summary()
                except Exception:
                    logger.exception("Daily summary failed")

        thread = threading.Thread(target=loop, daemon=True, name="daily-summary")
        thread.start()

    @staticmethod
    def _query_daily_stats() -> dict[str, Any] | None:
        """Query the database for today's stats and all-time totals.

        Runs async DB queries in a fresh event loop (safe from sync thread).
        Returns None if the DB is unavailable.
        """
        try:
            from .database import get_session_factory, reset_engine, compute_balances
            from .models import Sweep, RewardEntry, Claim, ClaimStatus
            from .config import settings
            from sqlalchemy import func, select
        except Exception:
            logger.debug("Cannot import DB modules for daily summary")
            return None

        async def _fetch():
            factory = get_session_factory()
            async with factory() as session:
                now = datetime.now(timezone.utc)
                # Summary fires at midnight — report on the day that just ended
                day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
                day_start = day_end - timedelta(days=1)
                # Previous day (for comparison)
                prev_day_start = day_start - timedelta(days=1)

                # ── Day's sweeps (all ~20 epochs) ─────────────────────────
                day_sweeps_ok = await session.execute(
                    select(
                        func.count(Sweep.id),
                        func.coalesce(func.sum(Sweep.alpha_amount), 0),
                    ).where(
                        Sweep.success == True,  # noqa: E712
                        Sweep.created_at >= day_start,
                        Sweep.created_at < day_end,
                    )
                )
                row = day_sweeps_ok.one()
                day_sweep_count = row[0]
                day_alpha_swept = float(row[1])

                day_sweeps_fail = await session.execute(
                    select(func.count(Sweep.id)).where(
                        Sweep.success == False,  # noqa: E712
                        Sweep.created_at >= day_start,
                        Sweep.created_at < day_end,
                    )
                )
                day_failed = day_sweeps_fail.scalar_one()

                # ── Day's commission & distribution ───────────────────────
                day_rewards = await session.execute(
                    select(
                        func.coalesce(func.sum(RewardEntry.commission_alpha), 0),
                        func.coalesce(func.sum(RewardEntry.net_alpha), 0),
                        func.count(func.distinct(RewardEntry.evm_address)),
                    ).where(
                        RewardEntry.created_at >= day_start,
                        RewardEntry.created_at < day_end,
                    )
                )
                rr = day_rewards.one()
                day_commission = float(rr[0])
                day_distributed = float(rr[1])
                day_active_miners = rr[2]

                # ── Day's claims ──────────────────────────────────────────
                day_claims_ok = await session.execute(
                    select(
                        func.count(Claim.id),
                        func.coalesce(func.sum(Claim.amount_alpha), 0),
                    ).where(
                        Claim.status == ClaimStatus.COMPLETED,
                        Claim.processed_at >= day_start,
                        Claim.processed_at < day_end,
                    )
                )
                cr = day_claims_ok.one()
                day_claims_count = cr[0]
                day_alpha_claimed = float(cr[1])

                day_claims_fail = await session.execute(
                    select(func.count(Claim.id)).where(
                        Claim.status == ClaimStatus.FAILED,
                        Claim.processed_at >= day_start,
                        Claim.processed_at < day_end,
                    )
                )
                day_claims_failed = day_claims_fail.scalar_one()

                # ── Previous day's sweeps (for comparison) ────────────────
                prev_sweeps = await session.execute(
                    select(
                        func.coalesce(func.sum(Sweep.alpha_amount), 0),
                    ).where(
                        Sweep.success == True,  # noqa: E712
                        Sweep.created_at >= prev_day_start,
                        Sweep.created_at < day_start,
                    )
                )
                prev_day_alpha = float(prev_sweeps.scalar_one())

                # ── All-time totals ───────────────────────────────────────
                all_swept = await session.execute(
                    select(
                        func.count(Sweep.id),
                        func.coalesce(func.sum(Sweep.alpha_amount), 0),
                    ).where(Sweep.success == True)  # noqa: E712
                )
                ar = all_swept.one()
                alltime_sweep_count = ar[0]
                alltime_alpha_swept = float(ar[1])

                alltime_commission_r = await session.execute(
                    select(func.coalesce(func.sum(RewardEntry.commission_alpha), 0))
                )
                alltime_commission = float(alltime_commission_r.scalar_one())

                alltime_distributed_r = await session.execute(
                    select(func.coalesce(func.sum(RewardEntry.net_alpha), 0))
                )
                alltime_distributed = float(alltime_distributed_r.scalar_one())

                alltime_unique_miners = await session.execute(
                    select(func.count(func.distinct(RewardEntry.evm_address)))
                )
                unique_miners = alltime_unique_miners.scalar_one()

                alltime_claims_r = await session.execute(
                    select(
                        func.count(Claim.id),
                        func.coalesce(func.sum(Claim.amount_alpha), 0),
                    ).where(Claim.status == ClaimStatus.COMPLETED)
                )
                acr = alltime_claims_r.one()
                alltime_claims = acr[0]
                alltime_claimed = float(acr[1])

                # ── Outstanding balance ───────────────────────────────────
                total_owed = alltime_distributed - alltime_claimed

                # ── 7-day average (7 days before the reported day) ────────
                week_start = day_start - timedelta(days=7)
                week_swept_r = await session.execute(
                    select(
                        func.coalesce(func.sum(Sweep.alpha_amount), 0),
                    ).where(
                        Sweep.success == True,  # noqa: E712
                        Sweep.created_at >= week_start,
                        Sweep.created_at < day_start,
                    )
                )
                week_alpha = float(week_swept_r.scalar_one())
                avg_7d = week_alpha / 7.0 if week_alpha > 0 else 0.0

                # ── First sweep date (for "running since") ────────────────
                first_sweep_r = await session.execute(
                    select(func.min(Sweep.created_at)).where(
                        Sweep.success == True  # noqa: E712
                    )
                )
                first_sweep_date = first_sweep_r.scalar_one()
                days_running = (
                    (now - first_sweep_date).days
                    if first_sweep_date
                    else 0
                )

            return {
                # Reported day
                "day_sweep_count": day_sweep_count,
                "day_sweep_failed": day_failed,
                "day_alpha_swept": day_alpha_swept,
                "day_commission": day_commission,
                "day_distributed": day_distributed,
                "day_active_miners": day_active_miners,
                "day_claims_count": day_claims_count,
                "day_claims_failed": day_claims_failed,
                "day_alpha_claimed": day_alpha_claimed,
                # Previous day (for delta)
                "prev_day_alpha": prev_day_alpha,
                # Report date
                "report_date": day_start,
                # All-time
                "alltime_sweep_count": alltime_sweep_count,
                "alltime_alpha_swept": alltime_alpha_swept,
                "alltime_commission": alltime_commission,
                "alltime_distributed": alltime_distributed,
                "unique_miners": unique_miners,
                "alltime_claims": alltime_claims,
                "alltime_claimed": alltime_claimed,
                "outstanding_balance": total_owed,
                # Rates
                "avg_7d_daily": avg_7d,
                # Meta
                "commission_rate": settings.commission_rate,
                "days_running": days_running,
            }

        reset_engine()
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_fetch())
        except Exception:
            logger.exception("Failed to query DB for daily summary")
            return None
        finally:
            loop.close()
            reset_engine()

    @staticmethod
    def _fmt(value: float, decimals: int = 2) -> str:
        """Format a number with comma separators."""
        return f"{value:,.{decimals}f}"

    @staticmethod
    def _delta_arrow(today: float, yesterday: float) -> str:
        """Return a delta indicator comparing today vs yesterday."""
        if yesterday <= 0:
            return ""
        diff = today - yesterday
        pct = (diff / yesterday) * 100
        if abs(pct) < 0.5:
            return "  _(flat)_"
        arrow = "▲" if diff > 0 else "▼"
        return f"  {arrow} {abs(pct):.0f}% vs yesterday"

    def _send_daily_summary(self) -> None:
        """Send daily summary queried from the database."""
        stats = self._query_daily_stats()

        if stats is None:
            # Fallback: send a minimal message indicating DB was unavailable
            self.send_message(
                ":warning: *Daily Summary — Principal Miner*\n"
                "Could not query database for daily stats.",
                level="error",
            )
            return

        # Use the actual report date from the query (the day that just ended)
        date_str = stats["report_date"].strftime("%A, %b %-d, %Y")

        # ── Day's activity section ────────────────────────────────────
        total_sweeps = stats["day_sweep_count"] + stats["day_sweep_failed"]
        sweep_status = (
            f"{stats['day_sweep_count']}/{total_sweeps}"
            if total_sweeps > 0
            else "—"
        )
        sweep_emoji = (
            ":white_check_mark:" if stats["day_sweep_failed"] == 0 and total_sweeps > 0
            else ":warning:" if stats["day_sweep_failed"] > 0
            else ""
        )

        total_claims = stats["day_claims_count"] + stats["day_claims_failed"]
        claims_display = (
            f"{stats['day_claims_count']}/{total_claims}"
            if total_claims > 0
            else "—"
        )

        delta_str = self._delta_arrow(
            stats["day_alpha_swept"], stats["prev_day_alpha"]
        )

        commission_pct = int(stats["commission_rate"] * 100)

        # ── Build message ─────────────────────────────────────────────
        lines = [
            f":bar_chart: *Daily Report — Principal Miner*",
            f":calendar: {date_str}",
            "",
            f"*Day's Activity*",
            f"├ Sweeps: {sweep_status} {sweep_emoji}  |  *{self._fmt(stats['day_alpha_swept'])} ALPHA* swept{delta_str}",
            f"├ Commission: {self._fmt(stats['day_commission'])} ALPHA ({commission_pct}%)",
            f"├ Distributed: {self._fmt(stats['day_distributed'])} ALPHA → {stats['day_active_miners']} miners",
            f"├ Claims: {claims_display}  |  {self._fmt(stats['day_alpha_claimed'])} ALPHA claimed",
        ]

        if stats["day_sweep_failed"] > 0:
            lines.append(
                f"└ :x: {stats['day_sweep_failed']} sweep failure(s)"
            )

        if stats["day_claims_failed"] > 0:
            lines.append(
                f"└ :x: {stats['day_claims_failed']} claim failure(s)"
            )

        # ── Lifetime section ──────────────────────────────────────────
        lines += [
            "",
            f"*Lifetime Totals* _(day {stats['days_running']})_",
            f"├ Swept: {self._fmt(stats['alltime_alpha_swept'])} ALPHA ({self._fmt(stats['alltime_sweep_count'], 0)} sweeps)",
            f"├ Distributed: {self._fmt(stats['alltime_distributed'])} ALPHA",
            f"├ Commission: {self._fmt(stats['alltime_commission'])} ALPHA",
            f"├ Claimed: {self._fmt(stats['alltime_claimed'])} ALPHA ({self._fmt(stats['alltime_claims'], 0)} claims)",
            f"└ Outstanding: *{self._fmt(stats['outstanding_balance'])} ALPHA*",
        ]

        # ── Rates section ─────────────────────────────────────────────
        lines += [
            "",
            f"*Earnings Rate*",
            f"├ This day: {self._fmt(stats['day_alpha_swept'])} ALPHA",
            f"└ 7d avg: {self._fmt(stats['avg_7d_daily'])} ALPHA/day",
        ]

        # ── Miners section ────────────────────────────────────────────
        lines += [
            "",
            f"*Miners:* {stats['day_active_miners']} active  |  {stats['unique_miners']} unique all-time",
        ]

        self.send_message("\n".join(lines), level="info")

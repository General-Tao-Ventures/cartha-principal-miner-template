"""HTTP client for fetching position data from Cartha Verifier API.

Ported and adapted from cartha-principal-rewards/verifier_client.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import settings


class VerifierError(RuntimeError):
    """Raised when the verifier cannot be reached or returns an error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Position:
    """Represents a federated miner's locked position.

    API Field Mapping:
        - is_active: Position in frozen epoch (currently earning rewards)
        - is_verified: Position in upcoming epoch (will earn next week)
        - original_amount_usdc -> scoring_amount: Amount used for scoring
        - amount_usdc -> withdrawable_amount: Current withdrawable amount
        - pending_lock_amount_usdc -> pending_amount: Mid-epoch top-ups
    """

    evm_address: str
    scoring_amount: float
    withdrawable_amount: float
    pending_amount: float
    lock_days: int
    pool_id: str
    expires_at: datetime
    is_verified: bool
    is_active: bool

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> Position:
        """Create Position from Verifier API response data."""
        expires_at_str = data.get("expires_at") or data.get("expiresAt")
        if expires_at_str:
            if isinstance(expires_at_str, str):
                if expires_at_str.endswith("Z"):
                    expires_at = datetime.fromisoformat(
                        expires_at_str.replace("Z", "+00:00")
                    )
                else:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                expires_at = datetime.fromtimestamp(expires_at_str, tz=timezone.utc)
        else:
            expires_at = datetime.min.replace(tzinfo=timezone.utc)

        def parse_amount(raw: Any) -> float:
            if raw is None:
                return 0.0
            if isinstance(raw, int) and raw > 1_000_000:
                return float(raw) / 1e6  # USDC 6 decimals
            return float(raw)

        scoring_raw = (
            data.get("original_amount_usdc")
            or data.get("originalAmountUsdc")
            or data.get("original_amount")
            or data.get("originalAmount")
            or data.get("amount_usdc")
            or data.get("amountUsdc")
            or data.get("amount", 0)
        )
        scoring_amount = parse_amount(scoring_raw)

        withdrawable_raw = (
            data.get("amount_usdc")
            or data.get("amountUsdc")
            or data.get("amount", 0)
        )
        withdrawable_amount = parse_amount(withdrawable_raw)

        pending_raw = (
            data.get("pending_lock_amount_usdc")
            or data.get("pendingLockAmountUsdc")
            or data.get("pending_lock_amount")
            or data.get("pendingLockAmount", 0)
        )
        pending_amount = parse_amount(pending_raw)

        return cls(
            evm_address=(
                data.get("evm_address")
                or data.get("evmAddress")
                or data.get("owner", "")
            ),
            scoring_amount=scoring_amount,
            withdrawable_amount=withdrawable_amount,
            pending_amount=pending_amount,
            lock_days=int(data.get("lock_days") or data.get("lockDays", 0)),
            pool_id=data.get("pool_id") or data.get("poolId", ""),
            expires_at=expires_at,
            is_verified=bool(data.get("is_verified", data.get("isVerified", True))),
            is_active=bool(data.get("is_active", data.get("isActive", True))),
        )

    def is_expired(self) -> bool:
        """Check if the position has expired."""
        return datetime.now(timezone.utc) >= self.expires_at


# ─── API Helpers ──────────────────────────────────────────────────────────────


def _build_url(path: str) -> str:
    base = settings.verifier_url.rstrip("/")
    return f"{base}{path}"


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Make HTTP request to verifier API."""
    url = _build_url(path)
    headers = {"Accept": "application/json"}

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, url, params=params, headers=headers)
    except httpx.TimeoutException as exc:
        raise VerifierError(f"Request to verifier timed out: {url}") from exc
    except httpx.RequestError as exc:
        raise VerifierError(f"Failed to reach verifier at {url}: {exc}") from exc

    try:
        data = response.json()
    except ValueError:
        data = None

    if response.status_code >= 400:
        if isinstance(data, dict):
            detail = data.get("detail") or data.get("error") or response.text
        else:
            detail = response.text or "Unknown verifier error"
        raise VerifierError(str(detail), status_code=response.status_code)

    if not isinstance(data, dict):
        raise VerifierError("Unexpected verifier response payload.")

    return data


# ─── Profile Sync ─────────────────────────────────────────────────────────────


def sync_profile_to_verifier() -> dict[str, Any]:
    """Push the miner's identity, terms, and commission to the Cartha Verifier.

    Only non-empty fields are included — the verifier keeps existing values
    for anything not sent, so operators only need to set what they want to
    change in .env.

    Called once on startup so the public miner list stays up-to-date.
    """
    # Build identity block (only non-empty fields)
    identity: dict[str, Any] = {}
    if settings.miner_name:
        identity["name"] = settings.miner_name
    if settings.miner_description:
        identity["description"] = settings.miner_description
    if settings.miner_website:
        identity["website"] = settings.miner_website
    if settings.miner_discord:
        identity["discord"] = settings.miner_discord
    if settings.miner_logo_url:
        identity["logo_url"] = settings.miner_logo_url

    # Build terms block (only set fields)
    terms: dict[str, Any] = {}
    if settings.payout_schedule:
        terms["payout_schedule"] = settings.payout_schedule
    if settings.min_lock_days is not None:
        terms["min_lock_days"] = settings.min_lock_days
    if settings.min_lock_amount_usdc is not None:
        terms["min_lock_amount_usdc"] = settings.min_lock_amount_usdc
    if settings.terms_text:
        terms["terms_text"] = settings.terms_text
    terms["accepts_new_miners"] = settings.accepts_new_miners

    # Commission
    commission: dict[str, Any] = {"rate": settings.commission_rate}

    # Home EVM address (first one if set)
    home_evm = settings.home_evm_addresses[0] if settings.home_evm_addresses else None

    payload: dict[str, Any] = {
        "hotkey": settings.miner_hotkey,
        "slot": settings.miner_slot,
    }
    if home_evm:
        payload["home_evm_address"] = home_evm
    if identity:
        payload["identity"] = identity
    if terms:
        payload["terms"] = terms
    payload["commission"] = commission

    url = _build_url("/v1/miner/principal/sync")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=payload, headers=headers)

        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise VerifierError(
                f"Profile sync failed ({response.status_code}): {detail}",
                status_code=response.status_code,
            )

        return response.json()
    except httpx.RequestError as exc:
        raise VerifierError(f"Could not reach verifier for profile sync: {exc}") from exc


# ─── Public API ───────────────────────────────────────────────────────────────


def fetch_miner_positions(*, hotkey: str, slot: int) -> list[Position]:
    """Fetch all positions for a principal miner's federated miners.

    Args:
        hotkey: Principal miner's Bittensor hotkey (SS58)
        slot: Subnet slot number (UID)

    Returns:
        List of all Position objects (active, inactive, verified, etc.)
    """
    data = _request(
        "GET",
        "/v1/miner/status",
        params={"hotkey": hotkey, "slot": str(slot)},
    )

    positions: list[Position] = []
    pools_data = data.get("pools") or []
    if not pools_data and "position" in data:
        pools_data = (data.get("position") or {}).get("pools") or []

    for pool_data in pools_data:
        try:
            position = Position.from_api_response(pool_data)
            positions.append(position)
        except (KeyError, ValueError, TypeError) as exc:
            import sys
            print(f"Warning: Skipping malformed position: {exc}", file=sys.stderr)
            continue

    return positions


def fetch_scoring_positions(*, hotkey: str, slot: int) -> list[Position]:
    """Fetch positions that are actively earning rewards.

    Only positions where is_active=True AND not expired are included.
    These are positions in the frozen epoch contributing to rewards this week.

    Args:
        hotkey: Principal miner's Bittensor hotkey (SS58)
        slot: Subnet slot number (UID)

    Returns:
        List of Position objects that are actively earning
    """
    all_positions = fetch_miner_positions(hotkey=hotkey, slot=slot)
    return [
        pos
        for pos in all_positions
        if pos.is_active and not pos.is_expired()
    ]

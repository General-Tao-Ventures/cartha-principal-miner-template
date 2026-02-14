"""Scoring helpers for calculating federated miner reward shares.

Ported from cartha-principal-rewards/scoring.py.
Adapts the scoring algorithm from cartha-validator.

Score formula: pool_weight * scoring_amount * lock_boost
    - pool_weight: multiplier per pool (default 1.0)
    - scoring_amount: frozen epoch amount (original_amount_usdc)
    - lock_boost: min(lock_days, 365) / 365
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import MAX_LOCK_DAYS

if TYPE_CHECKING:
    from .verifier_client import Position


# ─── Pool Weights ─────────────────────────────────────────────────────────────

# Default: all pools equal weight. Add overrides here if needed.
POOL_WEIGHTS: dict[str, float] = {}


def get_pool_weight(pool_id: str) -> float:
    """Get weight multiplier for a pool (default 1.0)."""
    return POOL_WEIGHTS.get(pool_id, 1.0)


# ─── Scoring ──────────────────────────────────────────────────────────────────


def calculate_lock_boost(lock_days: int) -> float:
    """Calculate lock duration boost factor (0 to 1).

    Proportional to lock duration, capped at MAX_LOCK_DAYS (365).
    """
    if MAX_LOCK_DAYS <= 0:
        return 1.0
    return min(lock_days, MAX_LOCK_DAYS) / MAX_LOCK_DAYS


def score_position(position: "Position") -> float:
    """Calculate raw score for a single position.

    Score = pool_weight * scoring_amount * lock_boost
    """
    pool_weight = get_pool_weight(position.pool_id)
    boost = calculate_lock_boost(position.lock_days)
    return pool_weight * position.scoring_amount * boost


@dataclass
class ScoredPosition:
    """A position with its calculated score and reward share."""

    position: "Position"
    raw_score: float
    share: float = 0.0
    reward_alpha: float = 0.0

    @property
    def evm_address(self) -> str:
        return self.position.evm_address

    @property
    def scoring_amount(self) -> float:
        return self.position.scoring_amount

    @property
    def lock_days(self) -> int:
        return self.position.lock_days

    @property
    def pool_id(self) -> str:
        return self.position.pool_id


@dataclass
class SegmentedScoring:
    """Scoring results split into home and guest segments.

    Home: principal miner's own positions (no commission)
    Guest: federated miners' positions (commission applies)
    """

    home_positions: list[ScoredPosition] = field(default_factory=list)
    guest_positions: list[ScoredPosition] = field(default_factory=list)

    home_total_score: float = 0.0
    guest_total_score: float = 0.0
    total_score: float = 0.0

    home_alpha: float = 0.0
    guest_alpha: float = 0.0
    commission_alpha: float = 0.0

    @property
    def home_share(self) -> float:
        if self.total_score <= 0:
            return 0.0
        return self.home_total_score / self.total_score

    @property
    def guest_share(self) -> float:
        if self.total_score <= 0:
            return 0.0
        return self.guest_total_score / self.total_score


def score_positions_segmented(
    positions: list["Position"],
    total_alpha: float,
    home_evm_addresses: list[str] | None = None,
    commission_rate: float = 0.0,
) -> SegmentedScoring:
    """Score positions split into home and guest segments.

    Home segment (principal's own positions):
        - Receives proportional share of total ALPHA
        - NO commission applied

    Guest segment (federated miners):
        - Commission applied to guest pool's share
        - Remaining ALPHA distributed proportionally among guests

    Args:
        positions: List of Position objects to score
        total_alpha: Total ALPHA to distribute
        home_evm_addresses: List of EVM addresses exempt from commission
        commission_rate: Commission rate for guest pool (0.0-1.0)
    """
    result = SegmentedScoring()

    if not positions:
        return result

    # Normalize home addresses for comparison
    home_addrs = set()
    if home_evm_addresses:
        home_addrs = {addr.lower().strip() for addr in home_evm_addresses}

    # Split and score
    for pos in positions:
        raw_score = score_position(pos)
        sp = ScoredPosition(position=pos, raw_score=raw_score)

        if pos.evm_address.lower() in home_addrs:
            result.home_positions.append(sp)
            result.home_total_score += raw_score
        else:
            result.guest_positions.append(sp)
            result.guest_total_score += raw_score

    result.total_score = result.home_total_score + result.guest_total_score

    if result.total_score <= 0:
        return result

    # Calculate ALPHA for each segment
    result.home_alpha = total_alpha * result.home_share
    guest_alpha_before_commission = total_alpha * result.guest_share

    # Apply commission to guest pool only
    result.commission_alpha = guest_alpha_before_commission * commission_rate
    result.guest_alpha = guest_alpha_before_commission - result.commission_alpha

    # Distribute within home segment
    if result.home_total_score > 0:
        for sp in result.home_positions:
            sp.share = sp.raw_score / result.home_total_score
            sp.reward_alpha = result.home_alpha * sp.share

    # Distribute within guest segment (after commission)
    if result.guest_total_score > 0:
        for sp in result.guest_positions:
            sp.share = sp.raw_score / result.guest_total_score
            sp.reward_alpha = result.guest_alpha * sp.share

    return result


def aggregate_by_address(
    scored_positions: list[ScoredPosition],
) -> dict[str, float]:
    """Aggregate rewards by EVM address (multiple positions combined)."""
    rewards: dict[str, float] = {}
    for sp in scored_positions:
        addr = sp.evm_address.lower()
        rewards[addr] = rewards.get(addr, 0.0) + sp.reward_alpha
    return rewards

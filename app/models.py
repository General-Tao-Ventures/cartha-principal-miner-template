"""SQLAlchemy models for the internal principal rewards system.

Tables:
    sweeps          – Record of each BT epoch sweep to aggregator
    reward_entries  – Immutable append-only ledger of per-address rewards
    claims          – Claim requests with lifecycle tracking
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


def utcnow() -> datetime:
    """UTC-aware now for default timestamps."""
    return datetime.now(timezone.utc)


# ─── Sweeps ───────────────────────────────────────────────────────────────────


class Sweep(Base):
    """Record of a single BT epoch sweep to the aggregator hotkey.

    The `alpha_amount` moved in this sweep IS the epoch's earnings.
    """

    __tablename__ = "sweeps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bt_epoch_block = Column(BigInteger, nullable=False, unique=True, index=True)
    alpha_amount = Column(Numeric(precision=24, scale=12), nullable=False, default=0)
    success = Column(Boolean, nullable=False, default=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    # Relationship
    reward_entries = relationship("RewardEntry", back_populates="sweep", lazy="selectin")

    def __repr__(self) -> str:
        return (
            f"<Sweep id={self.id} block={self.bt_epoch_block} "
            f"alpha={self.alpha_amount} success={self.success}>"
        )


# ─── Reward Entries ───────────────────────────────────────────────────────────


class RewardEntry(Base):
    """Immutable append-only ledger entry for a single EVM address in one sweep.

    SECURITY: This table should NEVER have UPDATE or DELETE operations
    executed through any API or application code.
    """

    __tablename__ = "reward_entries"
    __table_args__ = (
        UniqueConstraint("sweep_id", "evm_address", name="uq_sweep_evm"),
        Index("ix_reward_entries_evm_address", "evm_address"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sweep_id = Column(Integer, ForeignKey("sweeps.id"), nullable=False)
    evm_address = Column(String(42), nullable=False)  # lowercase
    gross_alpha = Column(Numeric(precision=24, scale=12), nullable=False, default=0)
    commission_alpha = Column(Numeric(precision=24, scale=12), nullable=False, default=0)
    net_alpha = Column(Numeric(precision=24, scale=12), nullable=False, default=0)
    raw_score = Column(Numeric(precision=24, scale=12), nullable=False, default=0)
    share = Column(Numeric(precision=18, scale=12), nullable=False, default=0)
    is_home = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    # Relationship
    sweep = relationship("Sweep", back_populates="reward_entries")

    def __repr__(self) -> str:
        return (
            f"<RewardEntry id={self.id} sweep={self.sweep_id} "
            f"evm={self.evm_address[:10]}... net={self.net_alpha}>"
        )


# ─── Claims ───────────────────────────────────────────────────────────────────


class ClaimStatus(str, enum.Enum):
    """Lifecycle states for a claim."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Claim(Base):
    """A federated miner's request to claim accumulated rewards.

    Status transitions: pending -> processing -> completed | failed
    A claim in 'processing' state locks the balance.
    """

    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_evm_address", "evm_address"),
        Index("ix_claims_status", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    evm_address = Column(String(42), nullable=False)  # lowercase
    bt_coldkey = Column(String(64), nullable=False)  # SS58 address
    amount_alpha = Column(Numeric(precision=24, scale=12), nullable=False)
    status = Column(
        Enum(ClaimStatus, name="claim_status", native_enum=False),
        nullable=False,
        default=ClaimStatus.PENDING,
    )
    tx_hash = Column(String(128), nullable=True)
    error = Column(Text, nullable=True)
    signature = Column(Text, nullable=False)  # EVM signature for audit
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<Claim id={self.id} evm={self.evm_address[:10]}... "
            f"amount={self.amount_alpha} status={self.status}>"
        )

"""Initial schema: sweeps, reward_entries, claims.

Revision ID: 001_initial
Create Date: 2026-02-09
"""

from alembic import op
import sqlalchemy as sa

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── sweeps ────────────────────────────────────────────────────────────
    op.create_table(
        "sweeps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bt_epoch_block", sa.BigInteger(), nullable=False),
        sa.Column("alpha_amount", sa.Numeric(precision=24, scale=12), nullable=False, server_default="0"),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bt_epoch_block"),
    )
    op.create_index("ix_sweeps_bt_epoch_block", "sweeps", ["bt_epoch_block"])

    # ── reward_entries ────────────────────────────────────────────────────
    op.create_table(
        "reward_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sweep_id", sa.Integer(), sa.ForeignKey("sweeps.id"), nullable=False),
        sa.Column("evm_address", sa.String(42), nullable=False),
        sa.Column("gross_alpha", sa.Numeric(precision=24, scale=12), nullable=False, server_default="0"),
        sa.Column("commission_alpha", sa.Numeric(precision=24, scale=12), nullable=False, server_default="0"),
        sa.Column("net_alpha", sa.Numeric(precision=24, scale=12), nullable=False, server_default="0"),
        sa.Column("raw_score", sa.Numeric(precision=24, scale=12), nullable=False, server_default="0"),
        sa.Column("share", sa.Numeric(precision=18, scale=12), nullable=False, server_default="0"),
        sa.Column("is_home", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sweep_id", "evm_address", name="uq_sweep_evm"),
    )
    op.create_index("ix_reward_entries_evm_address", "reward_entries", ["evm_address"])

    # ── claims ────────────────────────────────────────────────────────────
    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("evm_address", sa.String(42), nullable=False),
        sa.Column("bt_coldkey", sa.String(64), nullable=False),
        sa.Column("amount_alpha", sa.Numeric(precision=24, scale=12), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("tx_hash", sa.String(128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_claims_evm_address", "claims", ["evm_address"])
    op.create_index("ix_claims_status", "claims", ["status"])


def downgrade() -> None:
    op.drop_table("claims")
    op.drop_table("reward_entries")
    op.drop_table("sweeps")

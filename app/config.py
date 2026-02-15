"""Runtime configuration loaded from .env via pydantic-settings."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


# ─── Hardcoded constants (match cartha-validator 1:1) ─────────────────────────
MAX_LOCK_DAYS: int = 365
SUBNET_NETUID: int = 35


class Settings(BaseSettings):
    """All configuration loaded from environment variables / .env file."""

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = Field(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/principal_rewards",
        alias="DATABASE_URL",
        description="PostgreSQL connection string",
    )

    # ── Cartha Verifier API ───────────────────────────────────────────────
    verifier_url: str = Field(
        "https://api.cartha.finance",
        alias="VERIFIER_URL",
    )

    # ── Public API URL (synced to verifier — frontend fetches rich data from here)
    api_url: str = Field(
        "",
        alias="API_URL",
        description="Public URL of this miner's API (e.g. https://my-miner.example.com). "
                    "Synced to verifier so the frontend can fetch rewards, claims, and federated miner data.",
    )

    # ── Identity (shown on frontend, synced to verifier) ─────────────────
    miner_name: str = Field("", alias="MINER_NAME")
    miner_description: str = Field("", alias="MINER_DESCRIPTION")
    miner_website: str = Field("", alias="MINER_WEBSITE")
    miner_discord: str = Field("", alias="MINER_DISCORD")
    miner_logo_url: str = Field("", alias="MINER_LOGO_URL")
    miner_tags: list[str] = Field(
        default_factory=list,
        alias="MINER_TAGS",
        description='JSON list of display tags, e.g. ["Automated Distribution","Self-Service Claiming"]',
    )

    # ── Terms (shown on frontend, synced to verifier) ──────────────────
    payout_schedule: str = Field("", alias="PAYOUT_SCHEDULE")
    min_lock_days: int | None = Field(None, alias="MIN_LOCK_DAYS")
    min_lock_amount_usdc: int | None = Field(None, alias="MIN_LOCK_AMOUNT_USDC")
    terms_text: str = Field("", alias="TERMS_TEXT")
    accepts_new_miners: bool = Field(True, alias="ACCEPTS_NEW_MINERS")

    # ── Bittensor ─────────────────────────────────────────────────────────
    miner_slot: int = Field(0, alias="MINER_SLOT", description="Subnet slot UID")
    miner_hotkey: str = Field(..., alias="MINER_HOTKEY")
    miner_coldkey: str = Field(..., alias="MINER_COLDKEY")
    aggregator_hotkey: str = Field(..., alias="AGGREGATOR_HOTKEY")
    bt_wallet_name: str = Field("default", alias="BT_WALLET_NAME")
    bt_wallet_path: str = Field("", alias="BT_WALLET_PATH")
    bt_network: str = Field("finney", alias="BT_NETWORK")

    # ── Wallet Password ───────────────────────────────────────────────────
    wallet_password: str = Field("", alias="WALLET_PASSWORD")

    # ── Commission & Rewards ──────────────────────────────────────────────
    home_evm_addresses: list[str] = Field(
        default_factory=list,
        alias="HOME_EVM_ADDRESSES",
        description="JSON list of EVM addresses exempt from commission",
    )
    commission_rate: float = Field(0.05, alias="COMMISSION_RATE")
    min_claim_alpha: float = Field(0.001, alias="MIN_CLAIM_ALPHA")

    # ── GCP Secret Manager (optional) ─────────────────────────────────────
    gcp_secret_id: str = Field("", alias="GCP_SECRET_ID")
    gcp_project: str = Field("", alias="GCP_PROJECT")

    # ── Slack ─────────────────────────────────────────────────────────────
    slack_webhook_url: str = Field("", alias="SLACK_WEBHOOK_URL")
    slack_error_webhook_url: str = Field("", alias="SLACK_ERROR_WEBHOOK_URL")

    # ── Epoch Monitor ─────────────────────────────────────────────────────
    poll_interval: float = Field(30.0, alias="POLL_INTERVAL")

    # ── Security & Rate Limiting ──────────────────────────────────────────
    admin_api_key: str = Field(
        "",
        alias="ADMIN_API_KEY",
        description="API key for admin endpoints (empty = admin disabled)",
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: ["*"],
        alias="CORS_ORIGINS",
        description="JSON list of allowed CORS origins",
    )
    claim_cooldown_seconds: int = Field(
        60,
        alias="CLAIM_COOLDOWN_SECONDS",
        description="Minimum seconds between claims for the same address",
    )

    # ── Validators ────────────────────────────────────────────────────────

    @field_validator("miner_tags", mode="before")
    @classmethod
    def parse_miner_tags(cls, v: Any) -> list[str]:
        """Parse MINER_TAGS from JSON string if needed."""
        if isinstance(v, str):
            if not v.strip():
                return []
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(t).strip() for t in parsed if t]
                raise ValueError("MINER_TAGS must be a JSON array")
            except json.JSONDecodeError:
                # Comma-separated fallback: "Tag One,Tag Two"
                return [t.strip() for t in v.split(",") if t.strip()]
        if isinstance(v, list):
            return [str(t).strip() for t in v if t]
        return []

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        """Parse CORS_ORIGINS from JSON string if needed."""
        if isinstance(v, str):
            if not v.strip():
                return ["*"]
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(o).strip() for o in parsed if o]
                raise ValueError("CORS_ORIGINS must be a JSON array")
            except json.JSONDecodeError:
                # Single origin as plain string (e.g. "https://app.cartha.finance")
                return [v.strip()]
        if isinstance(v, list):
            return [str(o).strip() for o in v if o]
        return ["*"]

    @field_validator("home_evm_addresses", mode="before")
    @classmethod
    def parse_home_addresses(cls, v: Any) -> list[str]:
        """Parse HOME_EVM_ADDRESSES from JSON string if needed."""
        if isinstance(v, str):
            if not v.strip():
                return []
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [addr.lower().strip() for addr in parsed if addr]
                raise ValueError("HOME_EVM_ADDRESSES must be a JSON array")
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in HOME_EVM_ADDRESSES: {e}") from e
        if isinstance(v, list):
            return [str(addr).lower().strip() for addr in v if addr]
        return []

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


# Singleton – importable as `from app.config import settings`
settings = Settings()  # type: ignore[call-arg]

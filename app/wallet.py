"""Wallet password loader: env var first, GCP Secret Manager as optional fallback.

Fetches the wallet password and sets it in the environment so the
Bittensor SDK auto-unlocks without interactive prompt.

Priority:
  1. WALLET_PASSWORD env var (most common for VPS / Docker)
  2. GCP Secret Manager (optional, for cloud deployments)
"""

from __future__ import annotations

import os
from typing import Any

from .config import settings

try:
    from google.cloud import secretmanager
except ImportError:
    secretmanager = None  # type: ignore[assignment]

try:
    import bittensor as bt
    from bittensor_wallet.errors import KeyFileError, PasswordError
except ImportError:
    bt = None  # type: ignore[assignment]
    KeyFileError = Exception  # type: ignore[assignment,misc]
    PasswordError = Exception  # type: ignore[assignment,misc]


PASSWORD_ENV_VAR = "MINER_WALLET_PASSWORD"


# ─── GCP Secret Manager (optional) ───────────────────────────────────────────


def fetch_gcp_secret(
    secret_id: str | None = None,
    project: str | None = None,
    version: str = "latest",
) -> str:
    """Fetch a secret value from GCP Secret Manager.

    Args:
        secret_id: Secret ID or full resource name
        project: GCP project ID
        version: Secret version (default: "latest")

    Returns:
        Secret value as string
    """
    if secretmanager is None:
        raise RuntimeError(
            "google-cloud-secret-manager not installed. "
            "Install with: pip install google-cloud-secret-manager"
        )

    sid = secret_id or settings.gcp_secret_id
    proj = project or settings.gcp_project

    if not sid:
        raise RuntimeError("GCP_SECRET_ID is not configured")

    # Build the resource path
    if sid.startswith("projects/"):
        if "/versions/" in sid:
            secret_path = sid
        else:
            secret_path = f"{sid}/versions/{version}"
    else:
        if not proj:
            raise RuntimeError(
                "GCP_PROJECT must be set when GCP_SECRET_ID is not a full resource name"
            )
        secret_path = f"projects/{proj}/secrets/{sid}/versions/{version}"

    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(name=secret_path)
    password = response.payload.data.decode("utf-8").strip()

    if not password:
        raise RuntimeError(f"GCP secret {secret_path} is empty")

    return password


# ─── Wallet Unlock ────────────────────────────────────────────────────────────


def _apply_password_to_wallet(wallet: Any, password: str) -> None:
    """Store password for all keyfiles (coldkey + hotkey).

    This mirrors the pattern from bittensor wallet management.
    """
    for attr in ("coldkey_file", "hotkey_file"):
        try:
            keyfile = getattr(wallet, attr, None)
            if keyfile is None:
                continue
            if not keyfile.exists_on_device():
                continue
            if not keyfile.is_encrypted():
                continue

            if hasattr(keyfile, "save_password_to_env"):
                keyfile.save_password_to_env(password)
            else:
                env_attr = getattr(keyfile, "env_var_name", None)
                if env_attr:
                    env_var = env_attr() if callable(env_attr) else env_attr
                    os.environ[str(env_var)] = password
        except Exception:
            pass


def _create_wallet(
    wallet_name: str | None = None,
    wallet_path: str | None = None,
    hotkey: str = "default",
) -> Any:
    """Create a Bittensor wallet object."""
    if bt is None:
        raise RuntimeError("bittensor SDK not installed")

    name = wallet_name or settings.bt_wallet_name
    path = wallet_path or settings.bt_wallet_path or None

    if path:
        return bt.Wallet(name=name, path=path, hotkey=hotkey)
    return bt.Wallet(name=name, hotkey=hotkey)


def _unlock_wallet(wallet: Any) -> Any:
    """Unlock both coldkey and hotkey on a wallet."""
    try:
        wallet.unlock_coldkey()
    except PasswordError as e:
        raise RuntimeError(f"Invalid wallet password: {e}") from e
    except KeyFileError as e:
        raise RuntimeError(f"Coldkey file error: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to unlock coldkey: {e}") from e

    try:
        wallet.unlock_hotkey()
    except (PasswordError, KeyFileError):
        pass  # Hotkey may not be encrypted, that's fine
    except Exception:
        pass  # Non-critical: coldkey is enough for most operations

    return wallet


def load_wallet_from_env(
    wallet_name: str | None = None,
    wallet_path: str | None = None,
    hotkey: str = "default",
) -> Any:
    """Load wallet using WALLET_PASSWORD from settings or MINER_WALLET_PASSWORD env var.

    This is the primary unlock method for VPS / Docker deployments.
    """
    if bt is None:
        raise RuntimeError("bittensor SDK not installed")

    # Try WALLET_PASSWORD from settings first, then legacy env var
    password = settings.wallet_password or os.environ.get(PASSWORD_ENV_VAR)
    if not password:
        raise RuntimeError(
            "No wallet password configured. Set WALLET_PASSWORD in your .env file "
            "or MINER_WALLET_PASSWORD in the environment."
        )

    os.environ[PASSWORD_ENV_VAR] = password

    wallet = _create_wallet(wallet_name, wallet_path, hotkey)
    _apply_password_to_wallet(wallet, password)
    return _unlock_wallet(wallet)


def load_wallet_with_gcp_secret(
    wallet_name: str | None = None,
    wallet_path: str | None = None,
    hotkey: str = "default",
) -> Any:
    """Load and unlock a Bittensor wallet using GCP Secret Manager password.

    Optional fallback for cloud deployments. Requires:
      - google-cloud-secret-manager package
      - GCP_SECRET_ID and GCP_PROJECT env vars

    Returns:
        Unlocked Bittensor wallet object
    """
    if bt is None:
        raise RuntimeError("bittensor SDK not installed")

    password = fetch_gcp_secret()
    os.environ[PASSWORD_ENV_VAR] = password

    wallet = _create_wallet(wallet_name, wallet_path, hotkey)
    _apply_password_to_wallet(wallet, password)
    return _unlock_wallet(wallet)


def load_wallet(
    wallet_name: str | None = None,
    wallet_path: str | None = None,
    hotkey: str = "default",
) -> Any:
    """Load wallet with automatic password resolution.

    Tries in order:
      1. WALLET_PASSWORD env var (most common)
      2. GCP Secret Manager (optional cloud fallback)

    Returns:
        Unlocked Bittensor wallet object
    """
    # 1. Try env var first (most common for VPS / Docker)
    password = settings.wallet_password or os.environ.get(PASSWORD_ENV_VAR)
    if password:
        return load_wallet_from_env(wallet_name, wallet_path, hotkey)

    # 2. Try GCP Secret Manager as fallback
    if settings.gcp_secret_id:
        return load_wallet_with_gcp_secret(wallet_name, wallet_path, hotkey)

    raise RuntimeError(
        "No wallet password source configured. Set WALLET_PASSWORD in your .env "
        "file, or configure GCP_SECRET_ID + GCP_PROJECT for GCP Secret Manager."
    )

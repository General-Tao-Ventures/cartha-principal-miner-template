"""Bittensor transfer logic: sweep to aggregator + claim transfers.

Ported from cartha-principal-rewards/alpha_distributor.py and
liquidity_flow_controller/neuron/miner.py.
"""

from __future__ import annotations

from typing import Any

from .config import SUBNET_NETUID, settings

try:
    import bittensor as bt
except ImportError:
    bt = None  # type: ignore[assignment]


class TransferError(RuntimeError):
    """Raised when a BT transfer operation fails."""
    pass


def _check_bittensor() -> None:
    if bt is None:
        raise TransferError("bittensor SDK not installed")


# ─── Subtensor / Chain ────────────────────────────────────────────────────────


def get_subtensor(network: str = "finney") -> Any:
    """Get a Bittensor subtensor connection."""
    _check_bittensor()
    return bt.Subtensor(network=network)


def resolve_slot(hotkey_ss58: str, network: str = "finney") -> int:
    """Look up the miner's UID/slot on the Bittensor chain.

    Args:
        hotkey_ss58: Hotkey SS58 address
        network: Bittensor network

    Returns:
        UID / slot number

    Raises:
        TransferError: If hotkey is not registered
    """
    _check_bittensor()
    subtensor = None

    try:
        subtensor = get_subtensor(network)
        uid = subtensor.get_uid_for_hotkey_on_subnet(
            hotkey_ss58=hotkey_ss58, netuid=SUBNET_NETUID
        )
        if uid is not None and uid >= 0:
            return int(uid)
        raise TransferError(
            f"Hotkey {hotkey_ss58} not registered on subnet {SUBNET_NETUID}"
        )
    except TransferError:
        raise
    except Exception as e:
        raise TransferError(f"Failed to resolve slot: {e}") from e
    finally:
        if subtensor is not None:
            try:
                if hasattr(subtensor, "close"):
                    subtensor.close()
                del subtensor
            except Exception:
                pass


def get_stake_balance(
    hotkey: str,
    coldkey: str,
    network: str = "finney",
) -> float:
    """Get ALPHA stake balance on the subnet (netuid 35)."""
    _check_bittensor()
    subtensor = get_subtensor(network)
    try:
        stake_info = subtensor.get_stake_for_coldkey_and_hotkey(
            coldkey_ss58=coldkey, hotkey_ss58=hotkey
        )
        if not stake_info:
            return 0.0

        # SDK returns dict keyed by netuid -> StakeInfo
        if isinstance(stake_info, dict):
            si = stake_info.get(SUBNET_NETUID)
            if si is None:
                return 0.0
            # StakeInfo has .stake which is a Balance
            return float(si.stake.tao) if hasattr(si.stake, "tao") else float(si.stake)

        # Older SDK: direct Balance object
        if hasattr(stake_info, "tao"):
            return float(stake_info.tao)

        return float(stake_info)
    except Exception as e:
        raise TransferError(f"Failed to get stake balance: {e}") from e


# ─── Sweep ────────────────────────────────────────────────────────────────────


def sweep_to_aggregator(
    miner_hotkey: str | None = None,
    aggregator_hotkey: str | None = None,
    network: str | None = None,
    wallet: Any | None = None,
) -> tuple[bool, float]:
    """Move ALL stake from principal miner's hotkey to aggregator hotkey.

    The amount moved IS the epoch's earnings.

    Args:
        miner_hotkey: Miner hotkey SS58 (defaults to settings)
        aggregator_hotkey: Aggregator hotkey SS58 (defaults to settings)
        network: BT network (defaults to settings)
        wallet: BT wallet object (must be unlocked)

    Returns:
        Tuple of (success, amount_moved_alpha)
    """
    _check_bittensor()

    hotkey = miner_hotkey or settings.miner_hotkey
    agg_hotkey = aggregator_hotkey or settings.aggregator_hotkey
    net = network or settings.bt_network

    if wallet is None:
        raise TransferError("Wallet must be provided for sweep operations")

    subtensor = get_subtensor(net)

    # Get current stake on our subnet
    try:
        stake_result = subtensor.get_stake_for_coldkey_and_hotkey(
            coldkey_ss58=wallet.coldkey.ss58_address,
            hotkey_ss58=hotkey,
        )
        if isinstance(stake_result, dict):
            si = stake_result.get(SUBNET_NETUID)
            if si is None or float(si.stake.tao if hasattr(si.stake, "tao") else si.stake) <= 0:
                return (True, 0.0)
            amount = float(si.stake.tao) if hasattr(si.stake, "tao") else float(si.stake)
        elif stake_result is not None and hasattr(stake_result, "tao"):
            if stake_result.rao <= 0:
                return (True, 0.0)
            amount = float(stake_result.tao)
        else:
            return (True, 0.0)
    except Exception as e:
        raise TransferError(f"Failed to get stake balance: {e}") from e

    if hotkey == agg_hotkey:
        return (True, amount)

    try:
        success = subtensor.move_stake(
            wallet=wallet,
            origin_hotkey_ss58=hotkey,
            origin_netuid=SUBNET_NETUID,
            destination_hotkey_ss58=agg_hotkey,
            destination_netuid=SUBNET_NETUID,
            move_all_stake=True,
        )
        if success:
            return (True, amount)
        else:
            raise TransferError(
                f"move_stake failed: {hotkey[:16]}... -> {agg_hotkey[:16]}..."
            )
    except TransferError:
        raise
    except Exception as e:
        raise TransferError(f"Sweep failed: {e}") from e


# ─── Claim Transfer ──────────────────────────────────────────────────────────


def transfer_claim(
    recipient_coldkey: str,
    alpha_amount: float,
    aggregator_hotkey: str | None = None,
    network: str | None = None,
    wallet: Any | None = None,
) -> str:
    """Transfer ALPHA from aggregator to a federated miner's BT coldkey.

    The ALPHA is already on the aggregator hotkey from the daily sweep.

    Args:
        recipient_coldkey: Recipient's coldkey SS58 address
        alpha_amount: Amount to transfer
        aggregator_hotkey: Aggregator hotkey SS58
        network: BT network
        wallet: BT wallet (must be unlocked)

    Returns:
        Transaction hash string
    """
    _check_bittensor()

    agg_hotkey = aggregator_hotkey or settings.aggregator_hotkey
    net = network or settings.bt_network

    if wallet is None:
        raise TransferError("Wallet must be provided for transfer operations")

    subtensor = get_subtensor(net)

    try:
        # Get the actual stake balance to use the SDK's native Balance object
        # (avoids unit conversion issues with Balance.from_tao().set_unit())
        stake = subtensor.get_stake(
            coldkey_ss58=wallet.coldkey.ss58_address,
            hotkey_ss58=agg_hotkey,
            netuid=SUBNET_NETUID,
        )

        if stake is None or stake.rao <= 0:
            raise TransferError(
                f"No stake on aggregator hotkey {agg_hotkey[:16]}... to transfer from"
            )

        # Use the SDK Balance directly for the amount
        # If claiming less than full balance, create a proportional Balance
        if alpha_amount >= float(stake.tao):
            amount_balance = stake  # Transfer all
        else:
            amount_balance = bt.Balance.from_tao(alpha_amount).set_unit(SUBNET_NETUID)

        result = subtensor.transfer_stake(
            wallet=wallet,
            destination_coldkey_ss58=recipient_coldkey,
            hotkey_ss58=agg_hotkey,
            origin_netuid=SUBNET_NETUID,
            destination_netuid=SUBNET_NETUID,
            amount=amount_balance,
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )

        # bittensor v10+ returns ExtrinsicResponse with extrinsic_receipt
        if hasattr(result, "extrinsic_receipt") and result.extrinsic_receipt is not None:
            receipt = result.extrinsic_receipt
            # Use the SDK's get_extrinsic_identifier() which resolves
            # block_number from block_hash and extrinsic_idx from the block.
            # Returns format "blockNumber-extrinsicIdx" e.g. "7510574-22"
            # usable in Taostats / tao.app explorer URLs.
            try:
                return receipt.get_extrinsic_identifier()
            except Exception:
                pass
            # Fallback to extrinsic hash from receipt
            if getattr(receipt, "extrinsic_hash", None):
                return str(receipt.extrinsic_hash)

        # Check ExtrinsicResponse success flag
        if hasattr(result, "success") and not result.success:
            error_msg = getattr(result, "message", "unknown error")
            raise TransferError(f"transfer_stake failed: {error_msg}")

        # Legacy SDK fallback: check for .hash on result
        if hasattr(result, "hash"):
            return str(result.hash)

        raise TransferError("transfer_stake returned no receipt or hash")

    except TransferError:
        raise
    except Exception as e:
        raise TransferError(f"Claim transfer failed: {e}") from e

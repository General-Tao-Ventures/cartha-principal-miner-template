"""EVM signature verification for claim authentication.

Uses EIP-191 personal_sign to verify that the claim request
was signed by the owner of the EVM address.
"""

from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_defunct


def verify_claim_signature(
    evm_address: str,
    signature: str,
    message: str,
) -> str:
    """Verify an EVM signature and recover the signer address.

    Args:
        evm_address: Expected signer EVM address (lowercase)
        signature: Hex-encoded signature from personal_sign
        message: The original message that was signed

    Returns:
        Recovered signer address (checksummed)

    Raises:
        ValueError: If signature verification fails or address mismatch
    """
    # Add 0x prefix to signature if missing
    if not signature.startswith("0x"):
        signature = f"0x{signature}"

    msg = encode_defunct(text=message)

    try:
        recovered = Account.recover_message(msg, signature=signature)
    except Exception as e:
        raise ValueError(f"Invalid signature: {e}") from e

    if recovered.lower() != evm_address.lower():
        raise ValueError(
            f"Signature mismatch: recovered {recovered}, expected {evm_address}"
        )

    return recovered


def build_claim_message(
    amount: float,
    bt_coldkey: str,
    timestamp: int,
) -> str:
    """Build the standardized claim message for signing.

    This ensures both frontend and backend use the same message format.

    Args:
        amount: ALPHA amount to claim
        bt_coldkey: Bittensor coldkey SS58 address
        timestamp: Unix timestamp

    Returns:
        Message string to sign
    """
    return f"Claim {amount} ALPHA to {bt_coldkey} | {timestamp}"

"""Claim readiness.

Describes claimable things (faucets, airdrop claim contracts) and determines — using
read-only RPC simulation — whether they are available to a given wallet and what they
would cost.

This package contains no signing code and no private keys.
"""

from app.claims.abi import decode_revert_reason, encode_call, event_topic, selector
from app.claims.readiness import (
    ALREADY_CLAIMED,
    COOLDOWN,
    MANUAL_REQUIRED,
    NOT_ELIGIBLE,
    READY,
    UNSIMULATED,
    WINDOW_CLOSED,
    WINDOW_NOT_OPEN,
    ReadinessResult,
    check_readiness,
)

__all__ = [
    "ALREADY_CLAIMED",
    "COOLDOWN",
    "MANUAL_REQUIRED",
    "NOT_ELIGIBLE",
    "READY",
    "UNSIMULATED",
    "WINDOW_CLOSED",
    "WINDOW_NOT_OPEN",
    "ReadinessResult",
    "check_readiness",
    "decode_revert_reason",
    "encode_call",
    "event_topic",
    "selector",
]


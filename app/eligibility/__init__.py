"""Eligibility checking.

Public-data-only verification of requirements, plus the READY/PARTIAL/NOT_ELIGIBLE/
UNKNOWN rollup.
"""

from app.eligibility.engine import (
    FAIL,
    MANUAL_ONLY,
    NOT_ELIGIBLE,
    PARTIAL,
    PASS,
    READY,
    UNKNOWN,
    check_wallet_project,
    rollup,
    verify_requirement,
)

__all__ = [
    "FAIL",
    "MANUAL_ONLY",
    "NOT_ELIGIBLE",
    "PARTIAL",
    "PASS",
    "READY",
    "UNKNOWN",
    "check_wallet_project",
    "rollup",
    "verify_requirement",
]


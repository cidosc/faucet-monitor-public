"""Requirement verification.

Only conditions that can be judged from public RPC/API data are verifiable.
Everything else (Discord roles, GitHub account age, "did you bridge", social tasks)
is reported as UNKNOWN and left for a human — we never assume a pass we cannot prove.

Rollup semantics (matching the requested example output):
    READY         every required requirement PASSes
    PARTIAL       at least one PASS, but some FAIL/UNKNOWN remain
    NOT_ELIGIBLE  nothing passes among the required checks
    UNKNOWN       no requirement could be evaluated at all
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chains.registry import make_adapter
from app.logging_conf import get_logger
from app.models import (
    Chain,
    EligibilityResult,
    EligibilitySummary,
    Faucet,
    Project,
    Requirement,
    Wallet,
)

log = get_logger(__name__)

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

READY = "READY"
PARTIAL = "PARTIAL"
NOT_ELIGIBLE = "NOT_ELIGIBLE"

# Requirement types that CAN be verified from public data in v1.
AUTO_VERIFIABLE = {
    "NATIVE_BALANCE_MIN",
    "TX_COUNT_MIN",
    "HOLDS_ERC20_MIN",
    "HOLDS_ERC721",
    "CONTRACT_INTERACTION",
    "FAUCET_CLAIMED",
}

# Types we deliberately cannot verify; they always resolve to UNKNOWN.
MANUAL_ONLY = {
    "BRIDGED",
    "DISCORD_ROLE",
    "GITHUB_AGE",
    "SOCIAL_TASK",
    "CUSTOM_MANUAL",
}


@dataclass
class CheckOutcome:
    status: str
    observed: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


async def verify_requirement(
    session: AsyncSession,
    wallet: Wallet,
    project: Project,
    chain: Chain | None,
    req: Requirement,
) -> CheckOutcome:
    rtype = req.req_type
    params = req.params or {}

    if rtype in MANUAL_ONLY:
        return CheckOutcome(
            UNKNOWN,
            observed=None,
            evidence={"reason": "not_verifiable_from_public_data", "type": rtype},
        )

    if rtype not in AUTO_VERIFIABLE:
        return CheckOutcome(UNKNOWN, evidence={"reason": "unknown_requirement_type", "type": rtype})

    if chain is None:
        return CheckOutcome(UNKNOWN, evidence={"reason": "project_has_no_chain_configured"})

    # ── FAUCET_CLAIMED: answered from our own records, no RPC needed ──
    if rtype == "FAUCET_CLAIMED":
        from app.models import Claim

        stmt = (
            select(Claim.id)
            .join(Faucet, Faucet.id == Claim.faucet_id)
            .where(Faucet.project_id == project.id, Claim.wallet_id == wallet.id,
                   Claim.status == "SUCCESS")
            .limit(1)
        )
        found = (await session.execute(stmt)).scalar_one_or_none()
        return CheckOutcome(
            PASS if found else FAIL,
            observed="claimed" if found else "no successful claim recorded",
            evidence={"source": "internal_db"},
        )

    adapter = make_adapter(chain)
    if not adapter.configured:
        return CheckOutcome(
            UNKNOWN,
            evidence={"reason": "rpc_not_configured", "rpc_env": chain.rpc_url_env},
        )

    try:
        async with adapter:
            if rtype == "NATIVE_BALANCE_MIN":
                min_wei = int(Decimal(str(params.get("min_eth", 0))) * Decimal(10**18))
                res = await adapter.native_balance(wallet.address)
                if not res.ok:
                    return CheckOutcome(UNKNOWN, error=res.error, evidence=res.evidence)
                bal = int(res.value or 0)
                return CheckOutcome(
                    PASS if bal >= min_wei else FAIL,
                    observed=f"{Decimal(bal) / Decimal(10**18):.6f} {chain.native_symbol}",
                    evidence={**res.evidence, "min_required_wei": min_wei, "observed_wei": bal},
                )

            if rtype == "TX_COUNT_MIN":
                need = int(params.get("min_count", 1))
                res = await adapter.transaction_count(wallet.address)
                if not res.ok:
                    return CheckOutcome(UNKNOWN, error=res.error, evidence=res.evidence)
                count = int(res.value or 0)
                return CheckOutcome(
                    PASS if count >= need else FAIL,
                    observed=f"{count} transactions sent",
                    evidence={**res.evidence, "required": need, "observed": count},
                )

            if rtype in ("HOLDS_ERC20_MIN", "HOLDS_ERC721"):
                token = str(params.get("token", ""))
                if not token:
                    return CheckOutcome(UNKNOWN, evidence={"reason": "requirement_missing_token_param"})
                if rtype == "HOLDS_ERC721":
                    res = await adapter.erc721_balance(token, wallet.address)
                    if not res.ok:
                        return CheckOutcome(UNKNOWN, error=res.error, evidence=res.evidence)
                    n = int(res.value or 0)
                    return CheckOutcome(
                        PASS if n > 0 else FAIL, observed=f"{n} NFT(s)",
                        evidence={**res.evidence, "observed": n},
                    )
                res = await adapter.erc20_balance(token, wallet.address)
                if not res.ok:
                    return CheckOutcome(UNKNOWN, error=res.error, evidence=res.evidence)
                tb = res.value
                minimum = Decimal(str(params.get("min_amount", 0)))
                ok = tb is not None and tb.human >= minimum
                return CheckOutcome(
                    PASS if ok else FAIL,
                    observed=f"{tb.human:.6f} {tb.symbol or ''}".strip() if tb else "0",
                    evidence={**res.evidence, "min_required": str(minimum)},
                )

            if rtype == "CONTRACT_INTERACTION":
                contract = str(params.get("contract", "")).lower()
                if not contract:
                    return CheckOutcome(UNKNOWN, evidence={"reason": "requirement_missing_contract_param"})
                topic = "0x" + "0" * 24 + wallet.address.lower().replace("0x", "")
                latest = int((await adapter.block_number()).value or 0)
                lookback = int(params.get("lookback_blocks", 50_000))
                from_block = max(0, latest - lookback)
                res = await adapter.get_logs(
                    address=contract, topics=[None, topic], from_block=from_block, to_block="latest"
                )
                if not res.ok:
                    return CheckOutcome(UNKNOWN, error=res.error, evidence=res.evidence)
                logs = res.value or []
                return CheckOutcome(
                    PASS if logs else FAIL,
                    observed=f"{len(logs)} matching log(s) in last {lookback} blocks",
                    evidence={**res.evidence, "contract": contract, "from_block": from_block},
                )

    except NotImplementedError as exc:
        return CheckOutcome(UNKNOWN, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("requirement_check_failed", extra={"req": req.code, "wallet": wallet.label})
        return CheckOutcome(UNKNOWN, error=repr(exc))

    return CheckOutcome(UNKNOWN, evidence={"reason": "unhandled_type", "type": rtype})


def rollup(outcomes: list[CheckOutcome], required_flags: list[bool]) -> tuple[str, int, int, int]:
    relevant = [o for o, req in zip(outcomes, required_flags, strict=True) if req]
    if not relevant:
        relevant = outcomes
    if not relevant:
        return UNKNOWN, 0, 0, 0

    n_pass = sum(1 for o in relevant if o.status == PASS)
    n_fail = sum(1 for o in relevant if o.status == FAIL)
    n_unknown = sum(1 for o in relevant if o.status == UNKNOWN)

    if n_unknown == len(relevant):
        status = UNKNOWN
    elif n_pass == len(relevant):
        status = READY
    elif n_pass == 0:
        status = NOT_ELIGIBLE
    else:
        status = PARTIAL
    return status, n_pass, n_fail, n_unknown


async def check_wallet_project(
    session: AsyncSession, wallet: Wallet, project: Project
) -> dict[str, Any]:
    """Evaluate every requirement of a project for one wallet; persist results + summary."""
    chain: Chain | None = None
    if project.chain_id is not None:
        chain = (await session.execute(select(Chain).where(Chain.id == project.chain_id))).scalar_one_or_none()

    reqs = list(
        (
            await session.execute(
                select(Requirement)
                .where(Requirement.project_id == project.id)
                .order_by(Requirement.sort_order, Requirement.id)
            )
        ).scalars()
    )

    started = time.monotonic()
    outcomes: list[CheckOutcome] = []
    for req in reqs:
        outcome = await verify_requirement(session, wallet, project, chain, req)
        outcomes.append(outcome)
        session.add(
            EligibilityResult(
                wallet_id=wallet.id,
                requirement_id=req.id,
                status=outcome.status,
                observed_value=outcome.observed,
                evidence={**outcome.evidence, "error": outcome.error, "req_code": req.code},
                error=outcome.error,
            )
        )

    status, n_pass, n_fail, n_unknown = rollup(outcomes, [r.is_required for r in reqs])

    summary = (
        await session.execute(
            select(EligibilitySummary).where(
                EligibilitySummary.wallet_id == wallet.id,
                EligibilitySummary.project_id == project.id,
            )
        )
    ).scalar_one_or_none()
    if summary is None:
        summary = EligibilitySummary(wallet_id=wallet.id, project_id=project.id, status=status)
        session.add(summary)
    summary.status = status
    summary.pass_count = n_pass
    summary.fail_count = n_fail
    summary.unknown_count = n_unknown
    from datetime import UTC, datetime

    summary.checked_at = datetime.now(UTC)

    await session.flush()
    return {
        "wallet": wallet.label,
        "project": project.slug,
        "status": status,
        "pass": n_pass,
        "fail": n_fail,
        "unknown": n_unknown,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "checks": [
            {
                "code": r.code,
                "type": r.req_type,
                "status": o.status,
                "observed": o.observed,
                "error": o.error,
            }
            for r, o in zip(reqs, outcomes, strict=True)
        ],
    }


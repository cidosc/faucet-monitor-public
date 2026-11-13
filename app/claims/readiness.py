"""Claim readiness: decide whether a claim is *available to you*, and what it would cost.

**This module never signs and never broadcasts.** Every check is one of:
  * an `eth_call` (executes the claim in the node's memory, changes nothing)
  * an `eth_estimateGas` / `eth_gasPrice` (pure queries)
  * an HTTP GET against a public proof endpoint

The output is a description a human can act on: eligible or not, why not, exactly what
calldata would be sent, and what the gas would cost in USD. The wallet's private key is
not needed, not requested, and not present anywhere in this path.

Status vocabulary:
    READY            window open, not yet claimed, simulation succeeds
    ALREADY_CLAIMED  proven claimed, either by an existence check or a revert reason
    NOT_ELIGIBLE     simulation reverted for another reason (e.g. invalid proof)
    WINDOW_NOT_OPEN  the claim window has not started
    WINDOW_CLOSED    the claim window has passed
    UNSIMULATED      we have a URL/target but no callable signature to simulate
    UNKNOWN          could not determine (RPC error, missing config, ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.chains.registry import make_adapter
from app.claims.abi import (
    decode_bool,
    decode_revert_reason,
    decode_uint,
    encode_call,
    looks_already_claimed,
    selector,
)
from app.config import settings
from app.logging_conf import get_logger
from app.models import Chain, ClaimReadiness, ClaimTarget, Wallet
from app.pricing.base import compute_usd, get_price_provider

log = get_logger(__name__)

READY = "READY"
ALREADY_CLAIMED = "ALREADY_CLAIMED"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
WINDOW_NOT_OPEN = "WINDOW_NOT_OPEN"
WINDOW_CLOSED = "WINDOW_CLOSED"
COOLDOWN = "COOLDOWN"
MANUAL_REQUIRED = "MANUAL_REQUIRED"
UNSIMULATED = "UNSIMULATED"
UNKNOWN = "UNKNOWN"


@dataclass
class ReadinessResult:
    status: str
    wallet_id: int
    target_id: int
    window_open: bool | None = None
    already_claimed: bool | None = None
    simulated_ok: bool | None = None
    revert_reason: str | None = None
    gas_estimate: Decimal | None = None
    gas_price_wei: Decimal | None = None
    native_symbol: str | None = None
    estimated_usd: Decimal | None = None
    # Derived: current gas price in gwei, and whether it clears the user's thresholds.
    # Advisory only — it changes the wording of a notification, never an action.
    gas_gwei: Decimal | None = None
    gas_acceptable: bool | None = None
    calldata: str | None = None
    to_address: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_row(self) -> ClaimReadiness:
        return ClaimReadiness(
            wallet_id=self.wallet_id,
            claim_target_id=self.target_id,
            status=self.status,
            window_open=self.window_open,
            already_claimed=self.already_claimed,
            simulated_ok=self.simulated_ok,
            revert_reason=self.revert_reason,
            gas_estimate=self.gas_estimate,
            gas_price_wei=self.gas_price_wei,
            native_symbol=self.native_symbol,
            estimated_usd=self.estimated_usd,
            calldata=self.calldata,
            to_address=self.to_address,
            evidence=self.evidence,
            error=self.error,
        )


def _window_state(target: ClaimTarget, now: datetime) -> bool | None:
    """True/False/None(=unbounded on that side)."""
    start = target.window_start
    end = target.window_end
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start is not None and now < start:
        return False
    if end is not None and now > end:
        return False
    return True


def _dig(data: Any, dotted: str | None) -> Any:
    """Walk a dotted path like ``data.claims`` through nested dicts."""
    if not dotted:
        return data
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)]
        else:
            return None
    return cur


async def fetch_proof(target: ClaimTarget, address: str) -> dict[str, Any]:
    """Fetch a merkle proof / allocation record from the project's public endpoint.

    Uses the same fetch policy as everything else (timeouts, retries, UA, robots
    exemption for JSON APIs). Failure is reported, never guessed around.
    """
    if not target.proof_source_url:
        return {}
    url = target.proof_source_url.replace("{address}", address)
    url = url.replace("{address_lower}", address.lower())

    try:
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds, headers={"User-Agent": settings.http_user_agent}
        ) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return {"_error": f"proof_http_{resp.status_code}", "_url": url}
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"{type(exc).__name__}: {exc}", "_url": url}

    node = _dig(payload, target.proof_json_path)
    if node is None:
        return {"_error": f"proof_path_not_found: {target.proof_json_path}", "_url": url}

    amount = _dig(node, target.amount_field) if target.amount_field else None
    proof = _dig(node, target.proof_field) if target.proof_field else None
    if amount is None and proof is None:
        # Endpoint returned the address object itself.
        amount = node.get("amount") if isinstance(node, dict) else None
        proof = node.get("proof") if isinstance(node, dict) else None

    return {"amount": amount, "proof": proof, "_url": url, "_raw_keys":
            list(node.keys()) if isinstance(node, dict) else None}


def build_claim_calldata(target: ClaimTarget, address: str, proof_data: dict[str, Any]) -> str:
    """Map the signature's arguments onto values from the proof payload."""
    sig = target.claim_signature or ""
    if not sig:
        raise ValueError("no claim_signature configured")

    name_args = sig[sig.index("(") + 1 : sig.rindex(")")].strip()
    arg_types = [a.strip() for a in name_args.split(",")] if name_args else []

    values: list[Any] = []
    for t in arg_types:
        if t == "address":
            values.append(address)
        elif t.startswith("uint") or t.startswith("int"):
            if proof_data.get("amount") is None:
                raise ValueError("signature needs an amount but the proof source returned none")
            values.append(int(proof_data["amount"]))
        elif t == "bytes32[]":
            if not proof_data.get("proof"):
                raise ValueError("signature needs a merkle proof but none was returned")
            values.append(list(proof_data["proof"]))
        else:
            raise ValueError(f"unsupported claim argument type: {t}")
    return encode_call(sig, values)


async def _check_faucet_target(
    session: AsyncSession,
    wallet: Wallet,
    target: ClaimTarget,
    chain: Chain,
    res: ReadinessResult,
) -> ReadinessResult:
    """Combine the faucet's observed status with this wallet's eligibility.

    Still strictly read-only: no HTTP POST, no transaction, no key.
    """
    from sqlalchemy import select

    from app.models import EligibilitySummary, Faucet, Project, Requirement

    url = (target.claim_url or "").strip()
    faucet = (
        await session.execute(select(Faucet).where(Faucet.url == url))
    ).scalar_one_or_none()

    if faucet is None:
        res.status = UNSIMULATED
        res.evidence["reason"] = (
            "no probed faucet matches claim_url — add the URL to config/sources.yaml "
            "so the probe job can track it"
        )
        return res

    res.evidence["faucet_status"] = faucet.status
    res.evidence["faucet_cooldown_seconds"] = faucet.cooldown_seconds
    res.evidence["requires_captcha"] = faucet.requires_captcha
    res.evidence["requires_login"] = faucet.requires_login
    res.evidence["login_provider"] = faucet.login_provider
    res.evidence["cloudflare_challenge"] = faucet.cloudflare_challenge

    # Eligibility for this project, if any requirements are defined.
    project = (
        await session.execute(select(Project).where(Project.id == target.project_id))
    ).scalar_one_or_none()
    req_count = 0
    if project is not None:
        req_count = len(
            (
                await session.execute(
                    select(Requirement.id).where(Requirement.project_id == project.id)
                )
            ).scalars().all()
        )
    summary = (
        await session.execute(
            select(EligibilitySummary).where(
                EligibilitySummary.wallet_id == wallet.id,
                EligibilitySummary.project_id == target.project_id,
            )
        )
    ).scalar_one_or_none()
    if summary is not None:
        res.evidence["eligibility"] = {
            "status": summary.status,
            "pass": summary.pass_count,
            "fail": summary.fail_count,
            "unknown": summary.unknown_count,
        }

    # Order matters: a hard gate beats a transient state.
    if faucet.cloudflare_challenge or faucet.requires_captcha:
        res.status = MANUAL_REQUIRED
        res.evidence["reason"] = (
            f"human verification required ({faucet.captcha_type or 'cloudflare'}) — "
            "never automated"
        )
        return res

    if faucet.requires_login:
        res.status = MANUAL_REQUIRED
        res.evidence["reason"] = f"login required ({faucet.login_provider or 'unknown provider'})"
        return res

    if summary is not None and summary.status == "NOT_ELIGIBLE":
        res.status = NOT_ELIGIBLE
        res.evidence["reason"] = (
            f"{summary.fail_count} requirement(s) failed on {chain.key}"
        )
        return res

    if faucet.status == "COOLDOWN":
        res.status = COOLDOWN
        cd = faucet.cooldown_seconds
        res.evidence["reason"] = f"faucet reports cooldown{' ~' + str(cd) + 's' if cd else ''}"
        return res

    if faucet.status == "EMPTY":
        res.status = NOT_ELIGIBLE
        res.evidence["reason"] = "faucet is empty"
        return res

    if faucet.status == "OFFLINE":
        res.status = UNKNOWN
        res.evidence["reason"] = "faucet offline"
        return res

    if faucet.status == "AVAILABLE":
        res.status = READY
        res.evidence["reason"] = (
            "faucet is AVAILABLE and no blocker detected — open claim_url to claim"
        )
        if req_count:
            res.evidence["requirements_defined"] = req_count
        return res

    res.status = UNSIMULATED
    res.evidence["reason"] = f"faucet status is {faucet.status} — cannot determine readiness"
    return res


async def check_readiness(
    session: AsyncSession,
    wallet: Wallet,
    target: ClaimTarget,
    chain: Chain,
    *,
    now: datetime | None = None,
) -> ReadinessResult:
    now = now or datetime.now(UTC)
    res = ReadinessResult(status=UNKNOWN, wallet_id=wallet.id, target_id=target.id)
    res.evidence = {
        "kind": target.kind,
        "chain": chain.key,
        "claim_url": target.claim_url,
        "human_verified": target.human_verified,
    }

    # ── window ──
    window_open = _window_state(target, now)
    res.window_open = window_open
    if window_open is False:
        if target.window_start and now < (
            target.window_start if target.window_start.tzinfo else target.window_start.replace(tzinfo=UTC)
        ):
            res.status = WINDOW_NOT_OPEN
            res.evidence["reason"] = "window has not started"
        else:
            res.status = WINDOW_CLOSED
            res.evidence["reason"] = "window has ended"
        return res

    # ── HTTP faucets: we cannot call them, but we CAN give a real verdict ──
    # The faucet's own status comes from the probing job (which does the actual fetching),
    # and the wallet's eligibility comes from the eligibility job. Combining them answers
    # the question the user actually has: "can I claim this right now, and if not, why?".
    if target.kind == "FAUCET_HTTP":
        return await _check_faucet_target(session, wallet, target, chain, res)

    if target.kind in ("MANUAL_ONLY", "GASLESS_RELAYER") or not target.claim_signature:
        res.status = UNSIMULATED
        res.evidence["reason"] = (
            f"kind={target.kind} has no callable claim signature; "
            "monitored only — open claim_url to act"
        )
        return res

    if not target.contract_address:
        res.status = UNKNOWN
        res.error = "contract_address_missing"
        return res

    adapter = make_adapter(chain)
    if not adapter.configured:
        res.status = UNKNOWN
        res.error = f"rpc_not_configured ({chain.rpc_url_env})"
        return res

    try:
        async with adapter:
            # ── already-claimed existence check (cheapest, most definitive) ──
            if target.claimed_signature:
                try:
                    if target.claimed_arg == "index":
                        # No index configured -> skip rather than guess.
                        res.evidence["claimed_check"] = "skipped (index unknown)"
                    else:
                        data = encode_call(target.claimed_signature, [wallet.address])
                        cr = await adapter.call(target.contract_address, data)
                        if cr.ok:
                            claimed = decode_bool(cr.value)
                            if claimed is None:
                                claimed = bool(decode_uint(cr.value))
                            res.already_claimed = claimed
                            res.evidence["claimed_check"] = {
                                "selector": selector(target.claimed_signature),
                                "result": claimed,
                            }
                            if claimed:
                                res.status = ALREADY_CLAIMED
                                res.to_address = target.contract_address
                                return res
                        else:
                            res.evidence["claimed_check_error"] = cr.error
                except Exception as exc:  # noqa: BLE001
                    res.evidence["claimed_check_error"] = repr(exc)

            # ── proof ──
            proof_data = await fetch_proof(target, wallet.address)
            if proof_data.get("_error"):
                res.status = UNKNOWN
                res.error = proof_data["_error"]
                res.evidence["proof"] = proof_data
                return res
            res.evidence["proof"] = {
                k: v for k, v in proof_data.items() if k != "proof"
            }
            res.evidence["proof_len"] = len(proof_data.get("proof") or [])

            # ── build calldata ──
            calldata = build_claim_calldata(target, wallet.address, proof_data)
            res.calldata = calldata
            res.to_address = target.contract_address

            # ── simulate ──
            sim = await adapter.call(target.contract_address, calldata, sender=wallet.address)
            res.simulated_ok = sim.ok
            if not sim.ok:
                reason = decode_revert_reason(sim.error)
                res.revert_reason = reason
                res.status = ALREADY_CLAIMED if looks_already_claimed(reason) else NOT_ELIGIBLE
                if res.status == ALREADY_CLAIMED:
                    res.already_claimed = True
                res.evidence["simulation_error"] = (sim.error or "")[:300]
                return res

            # ── gas / cost ──
            est = await adapter.estimate_gas(
                target.contract_address, calldata, sender=wallet.address
            )
            gp = await adapter.gas_price()
            if est.ok and est.value is not None:
                res.gas_estimate = Decimal(int(est.value))
            if gp.ok and gp.value is not None:
                res.gas_price_wei = Decimal(int(gp.value))
            res.native_symbol = chain.native_symbol

            price = await get_price_provider().price_usd(chain.native_symbol)
            if res.gas_estimate is not None and res.gas_price_wei is not None:
                res.estimated_usd = compute_usd(
                    int(res.gas_estimate), int(res.gas_price_wei), price
                )
                res.evidence["price_source"] = get_price_provider().name
                res.evidence["native_price_usd"] = str(price) if price is not None else None

            # Advise on timing. Testnet gas costs nothing real, so it is always fine;
            # on mainnet compare against the configured thresholds. When the price is
            # unknown this stays None rather than pretending the cost is acceptable.
            if res.gas_price_wei is not None:
                res.gas_gwei = (res.gas_price_wei / Decimal(10**9)).quantize(Decimal("0.0001"))
            if chain.is_testnet:
                res.gas_acceptable = True
            elif res.gas_gwei is None:
                res.gas_acceptable = None
            else:
                ok_gwei = float(res.gas_gwei) <= settings.claim_gas_max_gwei
                ok_usd = (
                    res.estimated_usd is None
                    or float(res.estimated_usd) <= settings.claim_cost_max_usd
                )
                res.gas_acceptable = bool(ok_gwei and ok_usd)
            res.evidence["gas_gwei"] = str(res.gas_gwei) if res.gas_gwei is not None else None
            res.evidence["gas_acceptable"] = res.gas_acceptable
            res.evidence["thresholds"] = {
                "max_gwei": settings.claim_gas_max_gwei,
                "max_usd": settings.claim_cost_max_usd,
            }

            res.status = READY
            res.evidence["simulation"] = "eth_call succeeded"
            return res

    except Exception as exc:  # noqa: BLE001
        log.exception("claim_readiness_failed", extra={"wallet": wallet.label, "target": target.id})
        res.status = UNKNOWN
        res.error = repr(exc)
        return res


def should_notify(previous: str | None, current: str) -> bool:
    """Alert on becoming claimable, or on a change of verdict. Never re-alert a steady state."""
    if previous == current:
        return False
    return current in (READY, NOT_ELIGIBLE, ALREADY_CLAIMED)


"""Auto-claim policy gate.

Every automated claim must pass ALL NINE gates below. Any single failure returns a
`PolicyDecision(allowed=False, ...)` and the attempt is recorded as SKIPPED_POLICY.
There is no override, no "force" flag, and no code path that bypasses this function.

In v1 `autoclaim/adapters/` is empty, so gate 7 (a registered, tested adapter) is
vacuously false for every faucet on earth. That means even if GLOBAL_AUTO_CLAIM were
flipped on by mistake, no on-chain write could occur.

Gate 5 is the important one: `projects.automation_allowed` defaults to UNKNOWN and may
only be set to ALLOWED by a human who has read the project's Terms/Docs/FAQ and pasted
the quoted source into `automation_evidence`. The scanner NEVER sets it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging_conf import get_logger
from app.models import Faucet, Project, Wallet

log = get_logger(__name__)

GATE_NAMES = {
    0: "global_precondition",
    1: "faucet_is_official_source",
    2: "free_resource_only",
    3: "no_captcha",
    4: "no_cloudflare_or_bot_challenge",
    5: "project_rules_allow_automation",
    6: "no_real_funds_required",
    7: "stable_tested_adapter_exists",
    8: "cooldown_is_known",
    9: "retry_budget_not_exhausted",
}


@dataclass
class PolicyDecision:
    allowed: bool
    failed_gate: int | None = None
    failed_gate_name: str | None = None
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def skip_reason(self) -> str:
        if self.allowed:
            return "allowed"
        return f"gate{self.failed_gate}:{self.failed_gate_name}"


async def evaluate(
    session: AsyncSession,
    *,
    faucet: Faucet,
    project: Project,
    wallet: Wallet,
    chain: Any | None = None,
    attempt_count: int = 0,
    adapter_registered: bool = False,
    adapter_tested: bool = False,
    observed_captcha: bool = False,
) -> PolicyDecision:
    detail: dict[str, Any] = {}
    reasons: list[str] = []

    def deny(gate: int, why: str, **extra: Any) -> PolicyDecision:
        reasons.append(why)
        detail.update(extra)
        # `detail["gate_name"]` (set by callers) distinguishes the global preconditions,
        # which all share gate number 0.
        name = detail.pop("gate_name", None) or GATE_NAMES[gate]
        return PolicyDecision(
            allowed=False,
            failed_gate=gate,
            failed_gate_name=name,
            reasons=reasons,
            detail=detail,
        )

    # ── global master switches (outside the numbered gates, all must hold) ──
    if not settings.global_auto_claim:
        return deny(0, "GLOBAL_AUTO_CLAIM is false (master switch off)",
                    gate_name="global_switch")
    if settings.auto_claim_dry_run:
        reasons.append("AUTO_CLAIM_DRY_RUN is true — evaluating only, no action taken")
    if not wallet.auto_faucet:
        return deny(0, f"wallet '{wallet.label}' has auto_faucet=false",
                    gate_name="wallet_not_opted_in")
    if not wallet.is_test_wallet:
        return deny(0, f"wallet '{wallet.label}' is not flagged as a test wallet",
                    gate_name="wallet_not_test_wallet")
    if settings.auto_claim_require_allowlist and not faucet.auto_claim_allowlisted:
        return deny(0, "faucet is not on the auto-claim allowlist",
                    gate_name="not_allowlisted")

    # NOTE: gate 6 below requires a resolved testnet chain. Do not weaken this now that
    # mainnet chains are configured — a mainnet "claim" can cost real funds.

    # ── gate 1 ──
    if not faucet.is_official_source:
        return deny(1, "faucet.is_official_source is false (needs human confirmation)")
    detail["is_official_source"] = True

    # ── gate 2 ──
    if not faucet.is_free_resource:
        return deny(2, "faucet.is_free_resource is false (needs human confirmation)")
    detail["is_free_resource"] = True

    # ── gate 3 ──
    if faucet.requires_captcha or observed_captcha:
        return deny(3, f"captcha required ({faucet.captcha_type or 'observed'}) — never bypassed")
    detail["requires_captcha"] = False

    # ── gate 4 ──
    if faucet.cloudflare_challenge:
        return deny(4, "cloudflare/bot challenge detected — never bypassed")
    detail["cloudflare_challenge"] = False

    # ── gate 5 ──
    if project.automation_allowed != "ALLOWED":
        return deny(
            5,
            f"project.automation_allowed is '{project.automation_allowed}', not ALLOWED",
            automation_allowed=project.automation_allowed,
        )
    if not (project.automation_evidence or "").strip():
        return deny(5, "ALLOWED but automation_evidence is empty — no quoted source on file")
    detail["automation_evidence"] = project.automation_evidence

    # ── gate 6 ──
    # "No real funds required" is only provable if we know the chain is a TESTNET.
    # Checking merely that a chain is configured was insufficient: with mainnet chains
    # in config/chains.yaml, an unconfigured/unknown chain must fail closed.
    if chain is None:
        return deny(
            6,
            "project has no resolved chain — cannot prove testnet scope",
            gate_name="no_chain",
        )
    if not bool(getattr(chain, "is_testnet", False)):
        return deny(
            6,
            f"chain '{getattr(chain, 'key', '?')}' is a MAINNET — a claim could cost real funds",
            gate_name="mainnet_chain",
            chain_key=getattr(chain, "key", None),
        )
    if float(settings.auto_claim_max_usd) != 0.0:
        return deny(6, f"AUTO_CLAIM_MAX_USD is {settings.auto_claim_max_usd}, must be 0")
    detail["max_usd"] = settings.auto_claim_max_usd
    detail["chain_is_testnet"] = True

    # ── gate 7 ──
    if not adapter_registered:
        return deny(7, "no claim adapter registered for this faucet")
    if not adapter_tested:
        return deny(7, "adapter exists but is not marked tested")
    detail["adapter_registered"] = True

    # ── gate 8 ──
    if faucet.cooldown_seconds is None:
        return deny(8, "faucet cooldown is unknown — cannot schedule safely")
    detail["cooldown_seconds"] = faucet.cooldown_seconds

    # ── gate 9 ──
    if attempt_count >= settings.auto_claim_max_retries:
        return deny(9, f"retry budget exhausted ({attempt_count}/{settings.auto_claim_max_retries})")
    detail["attempt_count"] = attempt_count

    if settings.auto_claim_dry_run:
        return PolicyDecision(
            allowed=False,
            failed_gate=0,
            failed_gate_name="dry_run",
            reasons=reasons + ["all gates passed but AUTO_CLAIM_DRY_RUN=true"],
            detail=detail,
        )

    return PolicyDecision(allowed=True, reasons=["all 9 gates passed"], detail=detail)


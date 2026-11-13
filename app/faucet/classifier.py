"""Turn detector evidence into a single faucet status.

Status vocabulary (fixed):
    AVAILABLE | COOLDOWN | EMPTY | REQUIRE_MANUAL | CAPTCHA | OFFLINE | UNKNOWN

Rules that matter:
  * A network-level failure (timeout, DNS, connection reset) is NOT "OFFLINE". We cannot
    tell a dead faucet from our own flaky link, so it is reported as UNKNOWN with the
    error attached. OFFLINE is reserved for evidence that the *faucet* is down
    (5xx, gone, explicit maintenance notice).
  * Cloudflare / bot challenges are reported as CAPTCHA, with `cloudflare_challenge=true`
    and the exact block reason preserved in evidence. They are never retried around.
  * AVAILABLE means only: "reachable, presents a wallet-address form, no blocker
    detected". It is deliberately capped below certainty because we cannot confirm
    dispensing without a claim.
  * Anything below the confidence floor becomes UNKNOWN. We never guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.faucet.detectors import DetectorHit, run_all
from app.http import FetchResult

# ── status constants ──
AVAILABLE = "AVAILABLE"
COOLDOWN = "COOLDOWN"
EMPTY = "EMPTY"
REQUIRE_MANUAL = "REQUIRE_MANUAL"
CAPTCHA = "CAPTCHA"
OFFLINE = "OFFLINE"
UNKNOWN = "UNKNOWN"

ALL_STATUSES = {AVAILABLE, COOLDOWN, EMPTY, REQUIRE_MANUAL, CAPTCHA, OFFLINE, UNKNOWN}

CONFIDENCE_FLOOR = 0.50


@dataclass
class Classification:
    status: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    cooldown_seconds: int | None = None
    requires_wallet_address: bool | None = None
    requires_login: bool | None = None
    login_provider: str | None = None
    requires_captcha: bool | None = None
    captcha_type: str | None = None
    cloudflare_challenge: bool | None = None
    claim_amount: Decimal | None = None
    amount_unit: str | None = None


def _find(hits: list[DetectorHit], name: str) -> DetectorHit | None:
    for h in hits:
        if h.detector == name:
            return h
    return None


def classify(result: FetchResult) -> Classification:
    """Classify a fetch result. Never raises."""
    if result.robots_denied:
        return Classification(
            status=UNKNOWN,
            confidence=1.0,
            reasons=["disallowed_by_robots_txt"],
            evidence={"robots_denied": True},
        )

    # ── no HTTP response at all: our connectivity, not necessarily their availability ──
    if result.status is None:
        return Classification(
            status=UNKNOWN,
            confidence=0.9,
            reasons=[f"no_response: {result.error or 'unknown error'}"],
            evidence={"error": result.error, "attempts": result.attempts},
        )

    status = result.status
    raw = result.text or ""
    hits = run_all(raw, result.headers, status)
    evidence = {h.detector: h.as_dict() for h in hits if h.matched}

    cloudflare = _find(hits, "cloudflare")
    captcha = _find(hits, "captcha")
    login = _find(hits, "login")
    wallet_in = _find(hits, "wallet_input")
    cooldown = _find(hits, "cooldown")
    empty = _find(hits, "empty")
    offline = _find(hits, "offline")
    amount = _find(hits, "amount")

    claim_amount: Decimal | None = None
    amount_unit: str | None = None
    if amount and amount.matched:
        listed = amount.meta.get("amounts") or []
        if listed:
            try:
                claim_amount = Decimal(str(listed[0]["value"]))
                amount_unit = str(listed[0]["unit"])
            except (InvalidOperation, KeyError, TypeError):
                pass

    cooldown_seconds = None
    if cooldown and cooldown.matched:
        cooldown_seconds = cooldown.meta.get("cooldown_seconds")

    base = Classification(
        status=UNKNOWN,
        confidence=0.0,
        cooldown_seconds=cooldown_seconds,
        requires_wallet_address=bool(wallet_in and wallet_in.matched) or None,
        requires_login=bool(login and login.matched) or None,
        login_provider=(login.meta.get("provider") if login and login.matched else None),
        requires_captcha=bool(captcha and captcha.matched) or None,
        captcha_type=(captcha.meta.get("type") if captcha and captcha.matched else None),
        cloudflare_challenge=bool(cloudflare and cloudflare.matched) or None,
        claim_amount=claim_amount,
        amount_unit=amount_unit,
        evidence=evidence,
    )

    # ── 1. gone / server error ──
    # NOTE: order matters. A Cloudflare/CAPTCHA challenge is itself served with 403/503,
    # so the challenge checks below MUST run before the generic 5xx->OFFLINE rule.
    # Otherwise a challenge would be misreported as "the faucet is down", which is both
    # wrong and operationally misleading.
    if status in (404, 410):
        base.status, base.confidence = OFFLINE, 0.95
        base.reasons.append(f"url_gone (HTTP {status})")
        return base

    # ── 2. human-verification challenge ──
    if cloudflare and cloudflare.matched:
        base.status, base.confidence = CAPTCHA, cloudflare.confidence
        base.reasons.append(f"cloudflare_challenge_detected (HTTP {status})")
        return base
    if captcha and captcha.matched:
        base.status, base.confidence = CAPTCHA, captcha.confidence
        base.reasons.append(f"captcha_detected ({base.captcha_type})")
        return base

    # ── 3. plain server error (no challenge markers) ──
    if status >= 500:
        base.status, base.confidence = OFFLINE, 0.9
        base.reasons.append(f"server_error (HTTP {status})")
        return base

    # ── 4. access control without a solvable challenge ──
    if status in (401, 403, 406, 451):
        base.status, base.confidence = REQUIRE_MANUAL, 0.85
        base.reasons.append(f"access_control (HTTP {status}: {result.block_reason or 'blocked'})")
        return base

    # ── 4. explicit maintenance / paused ──
    if offline and offline.matched:
        base.status, base.confidence = OFFLINE, offline.confidence
        base.reasons.append("maintenance_or_paused_notice")
        return base

    # ── 5. login / social gating ──
    if login and login.matched:
        base.status, base.confidence = REQUIRE_MANUAL, login.confidence
        base.reasons.append(f"login_required ({base.login_provider or 'unknown provider'})")
        return base

    # ── 6. dry ──
    if empty and empty.matched:
        base.status, base.confidence = EMPTY, empty.confidence
        base.reasons.append("faucet_reports_empty")
        return base

    # ── 7. timed out for this user ──
    if cooldown and cooldown.matched:
        base.status, base.confidence = COOLDOWN, cooldown.confidence
        extra = f", ~{cooldown_seconds}s" if cooldown_seconds else ""
        base.reasons.append(f"cooldown_indicated{extra}")
        return base

    # ── 8. looks claimable ──
    if 200 <= status < 300:
        if wallet_in and wallet_in.matched:
            base.status = AVAILABLE
            # Capped: a reachable form with no blocker is strong, not conclusive evidence.
            base.confidence = min(0.70, wallet_in.confidence)
            base.reasons.append("wallet_address_form_present; no blocker detected")
            return base
        base.status, base.confidence = UNKNOWN, 0.6
        base.reasons.append("reachable_but_no_wallet_form_found")
        return base

    # ── 9. redirects / anything else ──
    base.status, base.confidence = UNKNOWN, 0.7
    base.reasons.append(f"unhandled_http_status ({status})")
    return base


def apply_confidence_floor(c: Classification) -> Classification:
    """Downgrade low-confidence verdicts to UNKNOWN. Applied by the probe before persisting."""
    if c.status != UNKNOWN and c.confidence < CONFIDENCE_FLOOR:
        c.reasons.append(
            f"downgraded_to_UNKNOWN (confidence {c.confidence:.2f} < {CONFIDENCE_FLOOR})"
        )
        c.status = UNKNOWN
    return c


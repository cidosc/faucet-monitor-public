"""Faucet probing: fetch -> classify -> persist (with evidence and de-duplication).

Writes a `faucet_checks` row only when something meaningful changed, or when the
previous observation is older than `HEARTBEAT_HOURS`. This keeps the table from
growing without bound on a 15-minute probe cadence while still proving liveness.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.faucet.classifier import Classification, apply_confidence_floor, classify
from app.http import Fetcher, FetchResult
from app.logging_conf import get_logger
from app.models import Faucet, FaucetCheck

log = get_logger(__name__)

HEARTBEAT_HOURS = 12


@dataclass
class ProbeOutcome:
    faucet_id: int
    url: str
    status: str
    previous_status: str | None
    confidence: float
    changed: bool
    status_changed: bool
    classification: Classification
    http_status: int | None
    error: str | None


def content_hash(body: str | None) -> str | None:
    if not body:
        return None
    return hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()


async def probe_one(fetcher: Fetcher, faucet: Faucet) -> tuple[Classification, FetchResult]:
    result = await fetcher.get(faucet.url)
    c = apply_confidence_floor(classify(result))
    return c, result


async def persist(
    session: AsyncSession,
    faucet: Faucet,
    classification: Classification,
    result: FetchResult,
    *,
    now: datetime | None = None,
) -> ProbeOutcome:
    now = now or datetime.now(UTC)
    previous = faucet.status
    new_hash = content_hash(result.text)

    changed = new_hash is not None and new_hash != faucet.content_hash
    status_changed = classification.status != previous

    faucet.previous_status = previous
    faucet.status = classification.status
    faucet.last_checked_at = now

    if classification.status == "AVAILABLE":
        faucet.last_available_at = now

    if classification.cooldown_seconds is not None:
        faucet.cooldown_seconds = classification.cooldown_seconds
    if classification.claim_amount is not None:
        faucet.claim_amount = classification.claim_amount
    if classification.amount_unit is not None:
        faucet.amount_unit = classification.amount_unit

    # Only overwrite these when the detector actually had an opinion (None == no observation).
    for attr in (
        "requires_wallet_address",
        "requires_login",
        "login_provider",
        "requires_captcha",
        "captcha_type",
        "cloudflare_challenge",
    ):
        value = getattr(classification, attr)
        if value is not None:
            setattr(faucet, attr, value)

    # NOTE: is_official_source / is_free_resource / automation_allowed are NEVER written
    # here. They require explicit human confirmation and are set only via the config
    # seed or the dashboard.

    if result.status is not None and result.status >= 500:
        faucet.consecutive_failures = (faucet.consecutive_failures or 0) + 1
    elif result.error is None:
        faucet.consecutive_failures = 0
    # Network errors do not increment consecutive_failures: that would blame the faucet
    # for our own link being down.

    faucet.content_hash = new_hash

    should_log = (
        status_changed
        or changed
        or faucet.last_checked_at is None
        or await _is_stale(session, faucet, now)
    )

    if should_log:
        session.add(
            FaucetCheck(
                faucet_id=faucet.id,
                checked_at=now,
                http_status=result.status,
                final_url=result.final_url,
                content_hash=new_hash,
                content_changed=changed,
                detected_status=classification.status,
                confidence=round(classification.confidence, 2),
                evidence={
                    "reasons": classification.reasons,
                    "detectors": classification.evidence,
                    "attempts": result.attempts,
                    "blocked": result.blocked,
                    "block_reason": result.block_reason,
                    "route": result.route,
                    "routes_tried": result.route_attempts,
                },
                error=result.error,
                duration_ms=result.elapsed_ms,
            )
        )

    return ProbeOutcome(
        faucet_id=faucet.id,
        url=faucet.url,
        status=classification.status,
        previous_status=previous,
        confidence=classification.confidence,
        changed=changed,
        status_changed=status_changed,
        classification=classification,
        http_status=result.status,
        error=result.error,
    )


async def _is_stale(session: AsyncSession, faucet: Faucet, now: datetime) -> bool:
    stmt = (
        select(FaucetCheck.checked_at)
        .where(FaucetCheck.faucet_id == faucet.id)
        .order_by(FaucetCheck.checked_at.desc())
        .limit(1)
    )
    last = (await session.execute(stmt)).scalar_one_or_none()
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return now - last > timedelta(hours=HEARTBEAT_HOURS)


async def probe_all(fetcher: Fetcher, session: AsyncSession, limit: int | None = None) -> list[ProbeOutcome]:
    stmt = select(Faucet).join(Faucet.project).where(Faucet.url.isnot(None)).order_by(Faucet.id)
    if limit:
        stmt = stmt.limit(limit)
    faucets = list((await session.execute(stmt)).scalars())
    outcomes: list[ProbeOutcome] = []
    for faucet in faucets:
        try:
            classification, result = await probe_one(fetcher, faucet)
            outcomes.append(await persist(session, faucet, classification, result))
        except Exception as exc:  # noqa: BLE001 - one bad faucet must not stop the sweep
            log.exception("probe_error", extra={"faucet_id": faucet.id, "url": faucet.url})
            outcomes.append(
                ProbeOutcome(
                    faucet_id=faucet.id, url=faucet.url, status="UNKNOWN",
                    previous_status=faucet.status, confidence=0.0, changed=False,
                    status_changed=False,
                    classification=Classification("UNKNOWN", 0.0, [f"probe_exception: {exc}"]),
                    http_status=None, error=repr(exc),
                )
            )
    await session.flush()
    return outcomes


def summarize(outcomes: list[ProbeOutcome]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for o in outcomes:
        by_status[o.status] = by_status.get(o.status, 0) + 1
    return {
        "items_found": len(outcomes),
        "items_updated": sum(1 for o in outcomes if o.status_changed),
        "error_count": sum(1 for o in outcomes if o.error),
        "by_status": by_status,
        "status_changes": [
            {"faucet_id": o.faucet_id, "from": o.previous_status, "to": o.status}
            for o in outcomes
            if o.status_changed
        ],
    }


"""Claim readiness job.

For every enabled claim target × wallet, determine whether the claim is available and
what it would cost — using read-only simulation only. Emits an alert when the verdict
changes to something actionable.

Nothing in this job can move funds: there is no signing key in this process, and no
code path here calls eth_sendRawTransaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.claims.readiness import check_readiness, should_notify
from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import Chain, ClaimReadiness, ClaimTarget, Project, Wallet
from app.notify.dedupe import dispatch_pending, enqueue
from app.notify.templates import claim_already, claim_not_available, claim_ready, reason_zh

log = get_logger(__name__)


async def _notify(
    session: Any, target: ClaimTarget, project: Project, chain: Chain,
    wallet: Wallet, res: Any
) -> bool:
    if res.status == "READY":
        msg = claim_ready(
            project=project.name,
            target=target.label,
            chain=chain.name,
            is_testnet=chain.is_testnet,
            wallet_label=wallet.label,
            wallet_address=wallet.address,
            gas_estimate=res.gas_estimate,
            gas_price_wei=res.gas_price_wei,
            estimated_usd=res.estimated_usd,
            claim_url=target.claim_url,
            to_address=res.to_address,
            calldata=res.calldata,
            gas_acceptable=res.gas_acceptable,
            max_gwei=settings.claim_gas_max_gwei,
            max_usd=settings.claim_cost_max_usd,
        )
    elif res.status == "ALREADY_CLAIMED":
        msg = claim_already(
            project=project.name,
            target=target.label,
            wallet_label=wallet.label,
            evidence=(
                res.revert_reason
                or str(res.evidence.get("claimed_check") or "存在性检查返回已领取")
            ),
        )
    else:
        msg = claim_not_available(
            project=project.name,
            target=target.label,
            wallet_label=wallet.label,
            status=res.status,
            reason=(
                reason_zh(res.revert_reason)
                if res.revert_reason
                else reason_zh(res.error)
                if res.error
                else reason_zh(res.evidence.get("reason"))
                if res.evidence.get("reason")
                else "未知"
            ),
        )

    today = datetime.now(UTC).strftime("%Y%m%d")
    return await enqueue(
        session,
        type=f"CLAIM_{res.status}",
        dedupe_key=f"claim:{target.id}:{wallet.id}:{res.status}:{today}",
        payload={"title": msg.title, "body": msg.body},
        project_id=project.id,
        wallet_id=wallet.id,
    )


async def check_claim_readiness() -> dict[str, Any]:
    checked = notified = errors = 0

    async with session_scope() as session:
        targets = list(
            (
                await session.execute(
                    select(ClaimTarget, Project, Chain)
                    .join(Project, Project.id == ClaimTarget.project_id)
                    .join(Chain, Chain.id == ClaimTarget.chain_id)
                    .where(ClaimTarget.enabled.is_(True))
                )
            ).all()
        )
        wallets = list(
            (await session.execute(select(Wallet).where(Wallet.enabled.is_(True)))).scalars()
        )

    if not targets:
        log.info("claim_readiness_skipped", extra={"reason": "no enabled claim targets"})
        return {"items_found": 0, "items_updated": 0,
                "note": "no claim targets configured — add one with `app.cli claim add`"}

    for target, project, chain in targets:
        for wallet in wallets:
            try:
                async with session_scope() as session:
                    fresh_target = (
                        await session.execute(
                            select(ClaimTarget).where(ClaimTarget.id == target.id)
                        )
                    ).scalar_one()
                    fresh_wallet = (
                        await session.execute(select(Wallet).where(Wallet.id == wallet.id))
                    ).scalar_one()

                    # Previous verdict, to alert only on change.
                    prev_row = (
                        await session.execute(
                            select(ClaimReadiness)
                            .where(
                                ClaimReadiness.wallet_id == wallet.id,
                                ClaimReadiness.claim_target_id == target.id,
                            )
                            .order_by(ClaimReadiness.checked_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    previous = prev_row.status if prev_row else None

                    res = await check_readiness(session, fresh_wallet, fresh_target, chain)
                    session.add(res.as_row())
                    await session.flush()
                    checked += 1

                    if should_notify(previous, res.status):
                        if await _notify(session, fresh_target, project, chain, fresh_wallet, res):
                            notified += 1
            except Exception:  # noqa: BLE001 - isolate per pair
                log.exception(
                    "claim_readiness_pair_failed",
                    extra={"target": target.id, "wallet": wallet.label},
                )
                errors += 1

    if notified:
        try:
            async with session_scope() as session:
                await dispatch_pending(session)
        except Exception:  # noqa: BLE001
            log.exception("claim_notify_dispatch_failed")

    summary = {
        "items_found": checked,
        "items_updated": checked,
        "error_count": errors,
        "notified": notified,
    }
    log.info("claim_readiness_complete", extra=summary)
    return summary


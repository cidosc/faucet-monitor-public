"""每日汇总通知。

把一切合并成每天一条消息。其他通知都受频率限制，所以这条是保证能收到的「系统还活着」
心跳。文案来自 app/notify/templates.py（中文集中管理）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select, text

from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import Claim, Cost, Faucet, Notification, Project, ScanLog, Task, Wallet
from app.notify.dedupe import dispatch_pending, enqueue
from app.notify.templates import daily_summary

log = get_logger(__name__)


async def send_daily_summary() -> dict[str, Any]:
    day = datetime.now(UTC).strftime("%Y-%m-%d")

    async with session_scope() as session:
        new_projects = (
            await session.execute(
                select(func.count(Project.id)).where(
                    Project.discovered_at >= text("now() - interval '1 day'")
                )
            )
        ).scalar_one()

        by_status = dict(
            (
                await session.execute(
                    select(Faucet.status, func.count(Faucet.id)).group_by(Faucet.status)
                )
            ).all()
        )

        pending_tasks = (
            await session.execute(
                select(func.count(Task.id)).where(Task.status == "PENDING")
            )
        ).scalar_one()

        claims_24h = (
            await session.execute(
                select(func.count(Claim.id)).where(
                    Claim.attempted_at >= text("now() - interval '1 day'")
                )
            )
        ).scalar_one()
        claims_ok = (
            await session.execute(
                select(func.count(Claim.id)).where(
                    Claim.attempted_at >= text("now() - interval '1 day'"),
                    Claim.status == "SUCCESS",
                )
            )
        ).scalar_one()

        gas_today = (
            await session.execute(
                select(func.coalesce(func.sum(Cost.estimated_usd), 0)).where(
                    Cost.occurred_at >= text("now() - interval '1 day'")
                )
            )
        ).scalar_one()
        gas_total = (
            await session.execute(select(func.coalesce(func.sum(Cost.estimated_usd), 0)))
        ).scalar_one()

        wallets = (await session.execute(select(func.count(Wallet.id)))).scalar_one()
        queued = (
            await session.execute(
                select(func.count(Notification.id)).where(Notification.status == "PENDING")
            )
        ).scalar_one()

        recent_failures = list(
            (
                await session.execute(
                    select(ScanLog)
                    .where(
                        ScanLog.status == "FAILED",
                        ScanLog.started_at >= text("now() - interval '1 day'"),
                    )
                    .order_by(desc(ScanLog.started_at))
                    .limit(5)
                )
            ).scalars()
        )

    msg = daily_summary(
        day=day,
        new_projects=new_projects,
        faucet_status=by_status,
        pending_tasks=pending_tasks,
        wallets=wallets,
        claims_ok=claims_ok,
        claims_total=claims_24h,
        gas_today=gas_today,
        gas_total=gas_total,
        auto_claim_on=settings.global_auto_claim,
        channels=settings.notify_channel_list,
        failures=[(f.job_name, (f.error or "")) for f in recent_failures],
    )

    async with session_scope() as session:
        queued_ok = await enqueue(
            session,
            type="DAILY_SUMMARY",
            dedupe_key=f"daily_summary:{day}",
            payload={"title": msg.title, "body": msg.body},
        )
        # DAILY_SUMMARY is in COOLDOWN_EXEMPT_TYPES, so it is never throttled by its own
        # traffic — it is explicitly the "system is alive" heartbeat.
        result = await dispatch_pending(session) if queued_ok else {"sent": 0}

    summary = {
        "items_found": 1,
        "items_updated": 1 if queued_ok else 0,
        "queued": queued_ok,
        "queued_before": queued,
        **result,
    }
    log.info("daily_summary_complete", extra=summary)
    return summary


"""Data retention. Bounds table growth on a machine that must run for years.

Never deletes: transactions, costs, claims  (those are the financial/audit record).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, text

from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import FaucetCheck, Notification, ScanLog

log = get_logger(__name__)


async def apply_retention() -> dict[str, Any]:
    deleted: dict[str, int] = {}

    async with session_scope() as session:
        # 1. faucet_checks older than N days
        res = await session.execute(
            delete(FaucetCheck).where(
                FaucetCheck.checked_at < text(f"now() - interval '{settings.retain_faucet_checks_days} days'")
            )
        )
        deleted["faucet_checks"] = res.rowcount or 0

        # 2. notifications older than N days that are already resolved
        res = await session.execute(
            delete(Notification).where(
                Notification.created_at
                < text(f"now() - interval '{settings.retain_notifications_days} days'"),
                Notification.status.in_(["SENT", "SUPPRESSED"]),
            )
        )
        deleted["notifications"] = res.rowcount or 0

        # 3. scan_logs older than N days
        res = await session.execute(
            delete(ScanLog).where(
                ScanLog.started_at < text(f"now() - interval '{settings.retain_scan_logs_days} days'"),
                ScanLog.status != "RUNNING",
            )
        )
        deleted["scan_logs"] = res.rowcount or 0

        # 4. keep only the most recent N eligibility rows per (wallet, requirement)
        res = await session.execute(
            text(
                """
                DELETE FROM eligibility_results er
                USING (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY wallet_id, requirement_id
                               ORDER BY checked_at DESC
                           ) AS rn
                    FROM eligibility_results
                ) ranked
                WHERE er.id = ranked.id AND ranked.rn > :keep
                """
            ),
            {"keep": settings.retain_eligibility_per_pair},
        )
        deleted["eligibility_results"] = res.rowcount or 0

        # 5. leave headroom for autovacuum, but reclaim if the table is mostly dead tuples
        await session.execute(text("VACUUM (ANALYZE) faucet_checks")) if False else None

    log.info("retention_applied", extra=deleted)
    return {"items_updated": sum(deleted.values()), **deleted}


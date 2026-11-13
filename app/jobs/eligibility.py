"""Eligibility job: evaluate every (wallet, project-with-requirements) pair."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.eligibility.engine import check_wallet_project
from app.logging_conf import get_logger
from app.models import Project, Requirement, Wallet

log = get_logger(__name__)


async def check_eligibility() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors = 0

    async with session_scope() as session:
        wallets = list(
            (await session.execute(select(Wallet).where(Wallet.enabled.is_(True)))).scalars()
        )
        project_ids = list(
            (await session.execute(select(Requirement.project_id).distinct())).scalars()
        )
        projects = list(
            (await session.execute(select(Project).where(Project.id.in_(project_ids)))).scalars()
        )

    if not wallets or not projects:
        log.info("eligibility_skipped", extra={"wallets": len(wallets), "projects": len(projects)})
        return {"items_found": 0, "items_updated": 0, "note": "no wallets or no requirements"}

    for wallet in wallets:
        for project in projects:
            try:
                async with session_scope() as session:
                    fresh_wallet = (
                        await session.execute(select(Wallet).where(Wallet.id == wallet.id))
                    ).scalar_one()
                    fresh_project = (
                        await session.execute(select(Project).where(Project.id == project.id))
                    ).scalar_one()
                    results.append(await check_wallet_project(session, fresh_wallet, fresh_project))
            except Exception:  # noqa: BLE001 - isolate per pair
                log.exception(
                    "eligibility_pair_failed",
                    extra={"wallet": wallet.label, "project": project.slug},
                )
                errors += 1

    summary = {
        "items_found": len(results),
        "items_updated": len(results),
        "error_count": errors,
        "ready": sum(1 for r in results if r["status"] == "READY"),
        "partial": sum(1 for r in results if r["status"] == "PARTIAL"),
        "not_eligible": sum(1 for r in results if r["status"] == "NOT_ELIGIBLE"),
        "unknown": sum(1 for r in results if r["status"] == "UNKNOWN"),
    }
    log.info("eligibility_complete", extra=summary)
    return summary


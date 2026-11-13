"""把扫描结果转换成给用户看的通知。

去重键按 UTC 日期分桶，所以一个水龙头一天之内反复在 AVAILABLE/OFFLINE 之间跳变，
每个类别每天最多只提醒一次，而不是刷屏。

所有文案都来自 app/notify/templates.py（中文，集中管理）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.faucet.probe import ProbeOutcome
from app.logging_conf import get_logger
from app.models import Chain, Faucet, Project
from app.notify.dedupe import dispatch_pending, enqueue
from app.notify.templates import (
    faucet_available,
    faucet_failed,
    new_faucet,
    reasons_zh,
    requirement_hint,
)

log = get_logger(__name__)

FAILURE_STATUSES = {"OFFLINE", "EMPTY"}


def _today() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


async def _queue(session: AsyncSession, msg: Any, *, dedupe_key: str, **kw: Any) -> bool:
    return await enqueue(
        session,
        type=msg.type,
        dedupe_key=dedupe_key,
        payload={"title": msg.title, "body": msg.body},
        **kw,
    )


async def emit_probe_events(session: AsyncSession, outcomes: list[ProbeOutcome]) -> int:
    queued = 0
    for o in outcomes:
        if not o.status_changed:
            continue
        if o.status == "AVAILABLE":
            queued += await _faucet_recovered(session, o)
        elif o.status in FAILURE_STATUSES:
            queued += await _faucet_failed(session, o)

    if queued:
        result = await dispatch_pending(session)
        log.info("probe_notifications", extra={"queued": queued, **result})
    return queued


async def _context(session: AsyncSession, faucet_id: int) -> tuple[Faucet, Project, Chain | None]:
    faucet = (await session.execute(select(Faucet).where(Faucet.id == faucet_id))).scalar_one()
    project = (
        await session.execute(select(Project).where(Project.id == faucet.project_id))
    ).scalar_one()
    chain = None
    if project.chain_id is not None:
        chain = (
            await session.execute(select(Chain).where(Chain.id == project.chain_id))
        ).scalar_one_or_none()
    return faucet, project, chain


async def _faucet_recovered(session: AsyncSession, o: ProbeOutcome) -> int:
    faucet, project, chain = await _context(session, o.faucet_id)
    msg = faucet_available(
        project=project.name,
        chain=chain.name if chain else "未知",
        is_testnet=chain.is_testnet if chain else None,
        faucet_url=faucet.url,
        previous=o.previous_status,
        confidence=o.confidence,
        requirement=requirement_hint(faucet),
        policy=project.automation_allowed,
    )
    ok = await _queue(
        session, msg,
        dedupe_key=f"faucet_available:{faucet.id}:{_today()}",
        project_id=project.id, faucet_id=faucet.id,
    )
    return 1 if ok else 0


async def _faucet_failed(session: AsyncSession, o: ProbeOutcome) -> int:
    faucet, project, _chain = await _context(session, o.faucet_id)
    reason = reasons_zh(o.classification.reasons) or (o.error or "未知原因")
    msg = faucet_failed(
        project=project.name,
        faucet_url=faucet.url,
        status=o.status,
        reason=reason,
        http_status=o.http_status,
        consecutive=faucet.consecutive_failures or 0,
    )
    ok = await _queue(
        session, msg,
        dedupe_key=f"faucet_failed:{faucet.id}:{o.status}:{_today()}",
        project_id=project.id, faucet_id=faucet.id,
    )
    return 1 if ok else 0


async def emit_new_faucet(
    session: AsyncSession, project: Project, chain: Chain | None, faucet_url: str
) -> bool:
    msg = new_faucet(
        project=project.name,
        chain=chain.name if chain else "未知",
        is_testnet=chain.is_testnet if chain else None,
        faucet_url=faucet_url,
        requirement="待首次探测",
        policy=project.automation_allowed,
    )
    return await _queue(
        session, msg,
        dedupe_key=f"new_faucet:{project.slug}:{faucet_url}",
        project_id=project.id,
    )


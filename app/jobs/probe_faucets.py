"""Probe every configured faucet and record evidence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.faucet.probe import probe_all, summarize
from app.http import Fetcher
from app.logging_conf import get_logger
from app.models import Faucet

log = get_logger(__name__)


async def probe_faucets() -> dict[str, Any]:
    async with session_scope() as session:
        count = (await session.execute(select(Faucet.id))).scalars().all()
        if not count:
            log.info("probe_skipped_no_faucets")
            return {"items_found": 0, "items_updated": 0}

        async with Fetcher() as fetcher:
            outcomes = await probe_all(fetcher, session)

    summary = summarize(outcomes)
    log.info("probe_complete", extra=summary)

    # Notifications are emitted from the same observed data, but never let a notification
    # failure lose the scan results above (already committed).
    try:
        from app.notify.events import emit_probe_events

        async with session_scope() as session:
            await emit_probe_events(session, outcomes)
    except Exception:  # noqa: BLE001
        log.exception("probe_events_failed")

    return summary


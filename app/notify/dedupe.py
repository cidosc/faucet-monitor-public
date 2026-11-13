"""Notification dedupe, rate limiting and delivery.

Three layers stop this from ever becoming a spam firehose:

1. `notifications.dedupe_key` is UNIQUE. Re-running a job after a crash cannot produce a
   second copy of an alert that was already queued — the INSERT simply conflicts and is
   dropped. This is what makes the pipeline safe across restarts.
2. `NOTIFY_MIN_INTERVAL_SECONDS` enforces a global gap between consecutive sends.
3. `NOTIFY_MAX_PER_HOUR` caps the hourly volume; anything beyond it is marked SUPPRESSED
   (not lost — it stays visible in the dashboard and rolls into the daily summary).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging_conf import get_logger
from app.models import Notification
from app.notify.base import Message, Notifier

log = get_logger(__name__)

# A notification waiting longer than this is folded into the daily summary instead of
# arriving late. The gate defers rather than drops, so this is the only lossy step.
STALE_AFTER_HOURS = 24
MAX_SEND_ATTEMPTS = 3

# Types that ignore the inter-message cooldown (they are heartbeats, not alerts).
COOLDOWN_EXEMPT_TYPES = {"DAILY_SUMMARY"}


def build_notifier(channel: str) -> Notifier | None:
    """Instantiate a notifier by channel name. Returns None if unconfigured."""
    if channel == "feishu":
        from app.notify.feishu import FeishuNotifier

        n = FeishuNotifier()
        return n if n.configured else None
    if channel == "telegram":
        from app.notify.telegram import TelegramNotifier

        n = TelegramNotifier()
        return n if n.configured else None
    log.warning("unknown_notify_channel", extra={"channel": channel})
    return None


def active_channels() -> list[str]:
    return [c for c in settings.notify_channel_list if build_notifier(c) is not None]


async def enqueue(
    session: AsyncSession,
    *,
    type: str,  # noqa: A002 - matches the DB column name
    dedupe_key: str,
    payload: dict[str, Any],
    channel: str | None = None,
    project_id: int | None = None,
    faucet_id: int | None = None,
    wallet_id: int | None = None,
) -> bool:
    """Queue a notification. Returns True if newly queued, False if it was a duplicate."""
    if not settings.notify_enabled:
        return False

    channel = channel or (settings.notify_channel_list[0] if settings.notify_channel_list else "feishu")

    stmt = (
        pg_insert(Notification)
        .values(
            type=type,
            dedupe_key=dedupe_key,
            channel=channel,
            project_id=project_id,
            faucet_id=faucet_id,
            wallet_id=wallet_id,
            payload=payload,
            status="PENDING",
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
        .returning(Notification.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        log.debug("notification_duplicate_suppressed", extra={"dedupe_key": dedupe_key})
        return False
    log.info("notification_queued", extra={"type": type, "dedupe_key": dedupe_key})
    return True


async def _global_gate(session: AsyncSession, now: datetime) -> tuple[bool, str | None]:
    """Returns (allowed, reason_if_blocked)."""
    # hourly cap
    since = now - timedelta(hours=1)
    sent_last_hour = (
        await session.execute(
            select(func.count(Notification.id)).where(
                Notification.sent_at.isnot(None), Notification.sent_at >= since
            )
        )
    ).scalar_one()
    if sent_last_hour >= settings.notify_max_per_hour:
        return False, f"hourly_cap_reached ({sent_last_hour}/{settings.notify_max_per_hour})"

    # minimum gap between sends
    last_sent = (
        await session.execute(select(func.max(Notification.sent_at)))
    ).scalar_one_or_none()
    if last_sent is not None:
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=UTC)
        elapsed = (now - last_sent).total_seconds()
        if elapsed < settings.notify_min_interval_seconds:
            return False, f"cooldown_active ({int(elapsed)}s < {settings.notify_min_interval_seconds}s)"

    return True, None


async def dispatch_pending(session: AsyncSession, limit: int = 20) -> dict[str, int]:
    """Send queued notifications, respecting the global gate.

    IMPORTANT: when the gate blocks, notifications stay PENDING rather than being
    discarded. The gate is a *rate* limit, not a relevance filter — silently dropping a
    FAUCET_AVAILABLE alert because it landed 40s after the previous one would lose
    information the user explicitly asked for. They are delivered on a later cycle.
    Only notifications that have been waiting longer than STALE_AFTER_HOURS are marked
    SUPPRESSED (they roll into the daily summary instead of arriving stale).

    A small set of types bypass the inter-message cooldown: the daily summary is the
    deliberate "system is alive" heartbeat and must not be throttled by its own traffic.
    """
    if not settings.notify_enabled:
        return {"sent": 0, "failed": 0, "suppressed": 0, "deferred": 0}

    now = datetime.now(UTC)
    pending = list(
        (
            await session.execute(
                select(Notification)
                .where(Notification.status == "PENDING")
                .order_by(Notification.created_at)
                .limit(limit)
            )
        ).scalars()
    )

    sent = failed = suppressed = deferred = 0
    for n in pending:
        created = n.created_at if n.created_at.tzinfo else n.created_at.replace(tzinfo=UTC)
        age_hours = (now - created).total_seconds() / 3600.0
        if age_hours > STALE_AFTER_HOURS:
            n.status = "SUPPRESSED"
            n.last_error = f"stale_after {age_hours:.1f}h — see daily summary"
            suppressed += 1
            continue

        bypass = n.type in COOLDOWN_EXEMPT_TYPES
        allowed, reason = (True, None) if bypass else await _global_gate(session, now)
        if not allowed:
            # Leave PENDING and stop this pass: retry on the next cycle once the
            # cooldown window has passed.
            n.last_error = f"deferred: {reason}"
            deferred += len([x for x in pending if x.status == "PENDING"])
            log.info("notifications_deferred", extra={"reason": reason})
            break

        notifier = build_notifier(n.channel)
        if notifier is None:
            n.status = "FAILED"
            n.last_error = f"channel_not_configured: {n.channel}"
            failed += 1
            continue

        msg = Message(
            title=str(n.payload.get("title", n.type)),
            body=str(n.payload.get("body", "")),
            type=n.type,
        )
        n.attempts = (n.attempts or 0) + 1
        result = await notifier.send(msg)

        if result.ok:
            n.status = "SENT"
            n.sent_at = datetime.now(UTC)
            n.last_error = None
            sent += 1
        else:
            if n.attempts >= MAX_SEND_ATTEMPTS:
                n.status = "FAILED"
            n.last_error = result.error
            failed += 1
            log.warning(
                "notification_send_failed",
                extra={"id": n.id, "channel": n.channel, "error": result.error,
                       "attempts": n.attempts},
            )
            break  # back off rather than hammer a failing endpoint

    await session.flush()
    return {"sent": sent, "failed": failed, "suppressed": suppressed, "deferred": deferred}


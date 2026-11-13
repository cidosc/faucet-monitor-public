"""APScheduler wiring.

Every job is wrapped by `run_guarded`, which provides:
  * a PostgreSQL advisory lock, so a job never runs concurrently with itself
    (matters across restarts and if a second replica/worker is ever added)
  * a hard timeout
  * a row in `scan_logs` for every run, success or failure
  * total exception isolation — one failing job can never kill the scheduler or the web app
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

JobFn = Callable[[], Awaitable[dict[str, Any] | None]]

_scheduler: AsyncIOScheduler | None = None


async def _acquire_lock(name: str) -> bool:
    """Try to take a session-level advisory lock. Returns False if another instance holds it."""
    from app.db import get_engine

    # hashtext() maps an arbitrary string to a stable int4 for the advisory lock space.
    async with get_engine().connect() as conn:
        result = await conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:n))"), {"n": name})
        ok = bool(result.scalar())
    return ok


async def _release_lock(name: str) -> None:
    from app.db import get_engine

    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT pg_advisory_unlock(hashtext(:n))"), {"n": name})


async def run_guarded(name: str, fn: JobFn, triggered_by: str = "scheduler") -> None:
    """Run one job with locking, timeout, logging and full exception isolation."""
    from app.db import get_engine

    if not await _acquire_lock(f"job:{name}"):
        log.info("job_skipped_locked", extra={"job": name})
        return

    started = datetime.now(UTC)
    log_id: int | None = None
    try:
        # Record the run first so a hard crash still leaves a RUNNING row to be found later.
        async with get_engine().begin() as conn:
            row = await conn.execute(
                text(
                    "INSERT INTO scan_logs (job_name, started_at, status, triggered_by) "
                    "VALUES (:j, now(), 'RUNNING', :t) RETURNING id"
                ),
                {"j": name, "t": triggered_by},
            )
            log_id = row.scalar()

        result = await asyncio.wait_for(fn(), timeout=settings.job_timeout_seconds)
        result = result or {}

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    "UPDATE scan_logs SET finished_at=now(), status='SUCCESS', duration_ms=:d, "
                    "items_found=:f, items_updated=:u, error_count=:e WHERE id=:i"
                ),
                {
                    "d": duration_ms,
                    "f": int(result.get("items_found", 0)),
                    "u": int(result.get("items_updated", 0)),
                    "e": int(result.get("error_count", 0)),
                    "i": log_id,
                },
            )
        log.info(
            "job_finished",
            extra={"job": name, "duration_ms": duration_ms, **{k: v for k, v in result.items()}},
        )

    except TimeoutError:
        await _mark_failed(log_id, name, f"timeout after {settings.job_timeout_seconds}s")
        log.error("job_timeout", extra={"job": name})
    except Exception as exc:  # noqa: BLE001 - isolation is the whole point
        await _mark_failed(log_id, name, repr(exc))
        log.exception("job_failed", extra={"job": name})
    finally:
        try:
            await _release_lock(f"job:{name}")
        except Exception:  # noqa: BLE001
            log.warning("job_unlock_failed", extra={"job": name})


async def _mark_failed(log_id: int | None, name: str, error: str) -> None:
    if log_id is None:
        return
    from app.db import get_engine

    try:
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    "UPDATE scan_logs SET finished_at=now(), status='FAILED', error=:e WHERE id=:i"
                ),
                {"e": error[:2000], "i": log_id},
            )
    except Exception:  # noqa: BLE001
        log.warning("job_log_write_failed", extra={"job": name})


def _wrap(name: str, fn: JobFn) -> Callable[[], Awaitable[None]]:
    async def _inner() -> None:
        await run_guarded(name, fn)

    _inner.__name__ = f"job_{name}"
    return _inner


def build_scheduler() -> AsyncIOScheduler:
    from app.jobs import JOBS

    sched = AsyncIOScheduler(
        timezone=settings.tz,
        job_defaults={
            "coalesce": True,           # after downtime, run once rather than N times
            "max_instances": 1,
            "misfire_grace_time": settings.scheduler_misfire_grace_seconds,
        },
    )

    triggers: dict[str, Any] = {
        "probe_faucets": IntervalTrigger(minutes=settings.faucet_probe_interval_min),
        "discover": IntervalTrigger(hours=settings.discover_interval_hours),
        "eligibility": IntervalTrigger(hours=settings.eligibility_interval_hours),
        "claim_readiness": IntervalTrigger(minutes=settings.claim_readiness_interval_min),
        "detect_incoming": IntervalTrigger(minutes=settings.detect_incoming_interval_min),
        "costs": IntervalTrigger(minutes=settings.cost_refresh_interval_min),
        "daily_summary": CronTrigger(hour=settings.daily_summary_hour, minute=0),
        "retention": IntervalTrigger(hours=settings.retention_interval_hours),
    }

    for name, fn in JOBS.items():
        trigger = triggers.get(name)
        if trigger is None:
            log.warning("job_has_no_trigger", extra={"job": name})
            continue
        sched.add_job(
            _wrap(name, fn),
            trigger=trigger,
            id=name,
            name=name,
            replace_existing=True,
        )

    return sched


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = build_scheduler()
    _scheduler.start()
    log.info(
        "scheduler_started",
        extra={"jobs": [j.id for j in _scheduler.get_jobs()], "tz": settings.tz},
    )
    return _scheduler


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


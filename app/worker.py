"""Scheduler-only entrypoint (used when the dashboard is disabled / split into a worker)."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.logging_conf import get_logger, setup_logging

log = get_logger(__name__)


async def _run() -> None:
    from app.db import dispose_engine, ping
    from app.scheduler import start_scheduler

    setup_logging()
    log.info("worker_starting")

    if not await ping():
        log.error("database_unreachable_at_startup")

    scheduler = start_scheduler()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    log.info("worker_stopping")
    scheduler.shutdown(wait=False)
    await dispose_engine()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()


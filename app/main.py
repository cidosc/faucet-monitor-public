"""FastAPI application: dashboard + JSON API + embedded scheduler."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app import __version__
from app.config import settings
from app.logging_conf import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from app.db import dispose_engine, ping

    log.info(
        "app_starting",
        extra={"version": __version__, "env": settings.app_env, "tz": settings.tz},
    )

    if not await ping():
        # Not fatal: the container healthcheck will report unhealthy and Docker will
        # restart us. Crashing here would just add noise.
        log.error("database_unreachable_at_startup")

    scheduler = None
    if settings.run_scheduler:
        from app.scheduler import start_scheduler

        scheduler = start_scheduler()
    else:
        log.info("scheduler_disabled")

    try:
        yield
    finally:
        if scheduler is not None:
            with contextlib.suppress(Exception):
                scheduler.shutdown(wait=False)
        await dispose_engine()
        log.info("app_stopped")


app = FastAPI(
    title="Faucet / Airdrop Opportunity Monitor",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, object]:
    from app.db import ping

    db_ok = await ping()
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "version": __version__}


# Dashboard is mounted after the health route so it can never shadow it.
from app.web.routes import router as web_router  # noqa: E402

app.include_router(web_router)


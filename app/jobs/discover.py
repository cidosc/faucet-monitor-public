"""Discovery job: run every source plugin, upsert candidates, queue alerts for new faucets.

Trust rules enforced here:
  * `is_official_source` / `is_free_resource` are NEVER set by this job. Only a human
    (via the manual YAML seed, or later the dashboard) may set them.
  * `automation_allowed` may be set ONLY from the manual source, and only alongside
    `automation_evidence`. Automated plugins cannot influence it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chains.registry import sync_chains, verify_all
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import Chain, Faucet, Project, Task
from app.notify.events import emit_new_faucet
from app.sources.registry import build_plugins

log = get_logger(__name__)


async def _upsert_project(
    session: AsyncSession, cand: Any, chains: dict[str, Chain]
) -> tuple[Project, bool]:
    project = (
        await session.execute(select(Project).where(Project.slug == cand.slug))
    ).scalar_one_or_none()

    chain = chains.get(cand.chain_key) if cand.chain_key else None
    created = project is None
    if project is None:
        project = Project(slug=cand.slug, name=cand.name, source_plugin=cand.source_plugin)
        session.add(project)

    # Identity fields always refresh; human-authored trust fields never do (except
    # from the manual source, handled by the caller).
    project.name = cand.name or project.name
    project.source_plugin = project.source_plugin or cand.source_plugin
    project.source_url = cand.source_url or project.source_url
    project.network = cand.network or project.network
    project.project_url = cand.project_url or project.project_url
    project.docs_url = cand.docs_url or project.docs_url
    project.github_url = cand.github_url or project.github_url
    if chain is not None:
        project.chain_id = chain.id
    if cand.source_plugin == "manual" and cand.notes:
        project.notes = cand.notes

    # Only the human-curated source may set automation policy, and only with evidence.
    if cand.source_plugin == "manual":
        policy = (cand.raw or {}).get("automation_allowed", "UNKNOWN")
        evidence = (cand.raw or {}).get("automation_evidence")
        if policy == "ALLOWED" and not (evidence or "").strip():
            log.warning(
                "manual_automation_allowed_without_evidence",
                extra={"slug": cand.slug},
            )
            policy = "UNKNOWN"
        if policy in ("ALLOWED", "MANUAL", "FORBIDDEN", "UNKNOWN"):
            project.automation_allowed = policy
            if evidence:
                project.automation_evidence = evidence

    await session.flush()
    return project, created


async def _upsert_faucets(
    session: AsyncSession, project: Project, urls: list[str]
) -> list[Faucet]:
    new: list[Faucet] = []
    for url in urls:
        exists = (
            await session.execute(
                select(Faucet).where(Faucet.project_id == project.id, Faucet.url == url)
            )
        ).scalar_one_or_none()
        if exists is None:
            faucet = Faucet(project_id=project.id, url=url, status="UNKNOWN")
            session.add(faucet)
            new.append(faucet)
    await session.flush()
    return new


async def discover_projects() -> dict[str, Any]:
    chains_info: dict[str, Any] = {}
    wallets_info: dict[str, Any] = {}
    async with session_scope() as session:
        chains_info = await sync_chains(session)
        # Wallets come from config/wallets.yaml, so adding an address is a reviewed,
        # versionable change rather than a manual DB edit.
        from app.wallets import sync_wallets_from_config

        wallets_info = await sync_wallets_from_config(session)
        chains = {
            c.key: c for c in (await session.execute(select(Chain))).scalars()
        }

    plugins = build_plugins()
    if not plugins:
        log.info("discover_no_plugins")
        return {"items_found": 0, "items_updated": 0}

    projects_created = faucets_created = tasks_created = 0
    errors = 0
    new_faucet_alerts: list[tuple[int, str]] = []

    for plugin in plugins:
        try:
            candidates = await plugin.discover()
        except Exception:  # noqa: BLE001 - one plugin must not break the others
            log.exception("source_plugin_discover_failed", extra={"plugin": plugin.name})
            errors += 1
            continue

        async with session_scope() as session:
            chains = {c.key: c for c in (await session.execute(select(Chain))).scalars()}
            for cand in candidates:
                try:
                    project, created = await _upsert_project(session, cand, chains)
                    projects_created += int(created)
                    new_faucets = await _upsert_faucets(session, project, cand.faucet_urls)
                    faucets_created += len(new_faucets)
                    for f in new_faucets:
                        new_faucet_alerts.append((project.id, f.url))

                    # A lead is only useful if a human sees it. File a manual task once,
                    # when the project is first discovered (re-runs stay idempotent).
                    if created and cand.task_title:
                        exists = (
                            await session.execute(
                                select(Task.id).where(
                                    Task.project_id == project.id,
                                    Task.title == cand.task_title[:400],
                                )
                            )
                        ).scalar_one_or_none()
                        if exists is None:
                            session.add(
                                Task(
                                    project_id=project.id,
                                    title=cand.task_title[:400],
                                    description=cand.notes,
                                    task_type="MANUAL_REQUIRED",
                                    status="PENDING",
                                    priority=1,
                                    source_url=cand.source_url,
                                )
                            )
                            tasks_created += 1
                except Exception:  # noqa: BLE001
                    log.exception("candidate_upsert_failed", extra={"slug": cand.slug})
                    errors += 1

    # Chain self-check: a wrong RPC silently producing bad eligibility data would be
    # worse than a loud failure.
    async with session_scope() as session:
        chain_checks = await verify_all(session)
    for c in chain_checks:
        if not c.get("ok"):
            log.warning(
                "chain_verification_failed",
                extra={"chain": c.get("key"), "error": c.get("error"),
                       "rpc_env": c.get("rpc_env")},
            )

    # Queue alerts for genuinely new faucets (dedupe_key makes this safe to re-run).
    if new_faucet_alerts:
        try:
            async with session_scope() as session:
                from app.models import Chain as _Chain

                for project_id, url in new_faucet_alerts:
                    project = (
                        await session.execute(select(Project).where(Project.id == project_id))
                    ).scalar_one()
                    chain = None
                    if project.chain_id:
                        chain = (
                            await session.execute(
                                select(_Chain).where(_Chain.id == project.chain_id)
                            )
                        ).scalar_one_or_none()
                    await emit_new_faucet(session, project, chain, url)
                from app.notify.dedupe import dispatch_pending

                await dispatch_pending(session)
        except Exception:  # noqa: BLE001
            log.exception("discover_notify_failed")

    summary = {
        "items_found": projects_created + faucets_created + tasks_created,
        "items_updated": projects_created,
        "error_count": errors,
        "projects_created": projects_created,
        "faucets_created": faucets_created,
        "tasks_created": tasks_created,
        "plugins": [p.name for p in plugins],
        "chains": chains_info,
        "wallets": wallets_info,
        "chain_checks": chain_checks,
    }
    log.info("discover_complete", extra={k: v for k, v in summary.items() if k != "chain_checks"})
    return summary


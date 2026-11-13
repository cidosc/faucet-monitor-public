"""Dashboard routes. Server-rendered Jinja2 — no JS build step, no CDN dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select, text

from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import (
    Chain,
    Claim,
    Cost,
    Faucet,
    FaucetCheck,
    Notification,
    Project,
    Requirement,
    ScanLog,
    Task,
    Transaction,
    Wallet,
)
from app.web.auth import require_auth

log = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    async with session_scope() as s:
        projects = (await s.execute(select(func.count(Project.id)))).scalar_one()
        faucets_by_status = dict(
            (
                await s.execute(
                    select(Faucet.status, func.count(Faucet.id)).group_by(Faucet.status)
                )
            ).all()
        )
        wallets = (await s.execute(select(func.count(Wallet.id)))).scalar_one()
        manual_tasks = (
            await s.execute(
                select(func.count(Task.id)).where(
                    Task.task_type == "MANUAL_REQUIRED", Task.status == "PENDING"
                )
            )
        ).scalar_one()
        auto_tasks = (
            await s.execute(
                select(func.count(Task.id)).where(
                    Task.task_type == "AUTO_ALLOWED", Task.status == "PENDING"
                )
            )
        ).scalar_one()
        total_gas = (await s.execute(select(func.coalesce(func.sum(Cost.estimated_usd), 0)))).scalar_one()

        claims_total = (await s.execute(select(func.count(Claim.id)))).scalar_one()
        claims_ok = (
            await s.execute(select(func.count(Claim.id)).where(Claim.status == "SUCCESS"))
        ).scalar_one()

        recent_tx = list(
            (
                await s.execute(
                    select(Transaction, Chain.name, Wallet.label)
                    .join(Chain, Chain.id == Transaction.chain_id, isouter=True)
                    .join(Wallet, Wallet.id == Transaction.wallet_id, isouter=True)
                    .order_by(desc(Transaction.created_at))
                    .limit(10)
                )
            ).all()
        )
        recent_claims = list(
            (
                await s.execute(
                    select(Claim, Faucet.url, Wallet.label, Project.name)
                    .join(Faucet, Faucet.id == Claim.faucet_id, isouter=True)
                    .join(Wallet, Wallet.id == Claim.wallet_id, isouter=True)
                    .join(Project, Project.id == Claim.project_id, isouter=True)
                    .order_by(desc(Claim.attempted_at))
                    .limit(10)
                )
            ).all()
        )
        recent_jobs = list(
            (
                await s.execute(
                    select(ScanLog).order_by(desc(ScanLog.started_at)).limit(8)
                )
            ).scalars()
        )
        pending_notifications = (
            await s.execute(
                select(func.count(Notification.id)).where(Notification.status == "PENDING")
            )
        ).scalar_one()

    success_rate = (claims_ok / claims_total * 100.0) if claims_total else None

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "projects": projects,
            "faucets_by_status": faucets_by_status,
            "wallets": wallets,
            "manual_tasks": manual_tasks,
            "auto_tasks": auto_tasks,
            "total_gas_usd": float(total_gas or 0),
            "claims_total": claims_total,
            "claims_ok": claims_ok,
            "success_rate": success_rate,
            "recent_tx": recent_tx,
            "recent_claims": recent_claims,
            "recent_jobs": recent_jobs,
            "pending_notifications": pending_notifications,
            "auto_claim_enabled": settings.global_auto_claim,
            "notify_channels": settings.notify_channel_list,
        },
    )


@router.get("/projects", response_class=HTMLResponse, include_in_schema=False)
async def projects(request: Request) -> HTMLResponse:
    async with session_scope() as s:
        rows = list(
            (
                await s.execute(
                    select(Project, Chain.name)
                    .join(Chain, Chain.id == Project.chain_id, isouter=True)
                    .order_by(Project.name)
                )
            ).all()
        )
        faucet_counts = dict(
            (
                await s.execute(
                    select(Faucet.project_id, func.count(Faucet.id)).group_by(Faucet.project_id)
                )
            ).all()
        )
    return templates.TemplateResponse(
        request, "projects.html", {"rows": rows, "faucet_counts": faucet_counts}
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse, include_in_schema=False)
async def project_detail(request: Request, project_id: int) -> HTMLResponse:
    async with session_scope() as s:
        project = (
            await s.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            return HTMLResponse("<h1>404 — project not found</h1>", status_code=404)

        chain = (
            await s.execute(select(Chain).where(Chain.id == project.chain_id))
        ).scalar_one_or_none() if project.chain_id else None

        faucets = list(
            (await s.execute(select(Faucet).where(Faucet.project_id == project_id))).scalars()
        )
        requirements = list(
            (
                await s.execute(
                    select(Requirement)
                    .where(Requirement.project_id == project_id)
                    .order_by(Requirement.sort_order, Requirement.id)
                )
            ).scalars()
        )
        recent_checks = list(
            (
                await s.execute(
                    select(FaucetCheck)
                    .join(Faucet, Faucet.id == FaucetCheck.faucet_id)
                    .where(Faucet.project_id == project_id)
                    .order_by(desc(FaucetCheck.checked_at))
                    .limit(15)
                )
            ).scalars()
        )

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "project": project,
            "chain": chain,
            "faucets": faucets,
            "requirements": requirements,
            "recent_checks": recent_checks,
        },
    )


@router.get("/wallets", response_class=HTMLResponse, include_in_schema=False)
async def wallets(request: Request) -> HTMLResponse:
    async with session_scope() as s:
        rows = list(
            (
                await s.execute(
                    select(Wallet, Chain.name, Chain.native_symbol)
                    .join(Chain, Chain.id == Wallet.chain_id, isouter=True)
                    .order_by(Wallet.label)
                )
            ).all()
        )
        gas_by_wallet = dict(
            (
                await s.execute(
                    select(Cost.wallet_id, func.coalesce(func.sum(Cost.estimated_usd), 0)).group_by(
                        Cost.wallet_id
                    )
                )
            ).all()
        )
        claims_by_wallet = dict(
            (
                await s.execute(
                    select(Claim.wallet_id, func.count(Claim.id)).group_by(Claim.wallet_id)
                )
            ).all()
        )
        tx_by_wallet = dict(
            (
                await s.execute(
                    select(Transaction.wallet_id, func.count(Transaction.id)).group_by(
                        Transaction.wallet_id
                    )
                )
            ).all()
        )
    return templates.TemplateResponse(
        request,
        "wallets.html",
        {
            "rows": rows,
            "gas_by_wallet": gas_by_wallet,
            "claims_by_wallet": claims_by_wallet,
            "tx_by_wallet": tx_by_wallet,
        },
    )


@router.get("/transactions", response_class=HTMLResponse, include_in_schema=False)
async def transactions(request: Request) -> HTMLResponse:
    async with session_scope() as s:
        rows = list(
            (
                await s.execute(
                    select(Transaction, Chain.name, Wallet.label, Project.name, Cost.estimated_usd)
                    .join(Chain, Chain.id == Transaction.chain_id, isouter=True)
                    .join(Wallet, Wallet.id == Transaction.wallet_id, isouter=True)
                    .join(Project, Project.id == Transaction.project_id, isouter=True)
                    .join(Cost, Cost.transaction_id == Transaction.id, isouter=True)
                    .order_by(desc(Transaction.created_at))
                    .limit(200)
                )
            ).all()
        )
    return templates.TemplateResponse(request, "transactions.html", {"rows": rows})


@router.get("/tasks", response_class=HTMLResponse, include_in_schema=False)
async def tasks(request: Request) -> HTMLResponse:
    async with session_scope() as s:
        rows = list(
            (
                await s.execute(
                    select(Task, Project.name, Wallet.label)
                    .join(Project, Project.id == Task.project_id, isouter=True)
                    .join(Wallet, Wallet.id == Task.wallet_id, isouter=True)
                    .order_by(desc(Task.priority), desc(Task.discovered_at))
                    .limit(200)
                )
            ).all()
        )
    return templates.TemplateResponse(request, "tasks.html", {"rows": rows})


@router.get("/claims", response_class=HTMLResponse, include_in_schema=False)
async def claims(request: Request) -> HTMLResponse:
    async with session_scope() as s:
        rows = list(
            (
                await s.execute(
                    select(Claim, Faucet.url, Wallet.label, Project.name)
                    .join(Faucet, Faucet.id == Claim.faucet_id, isouter=True)
                    .join(Wallet, Wallet.id == Claim.wallet_id, isouter=True)
                    .join(Project, Project.id == Claim.project_id, isouter=True)
                    .order_by(desc(Claim.attempted_at))
                    .limit(200)
                )
            ).all()
        )
    return templates.TemplateResponse(request, "claims.html", {"rows": rows})


@router.get("/status", response_class=HTMLResponse, include_in_schema=False)
async def status_page(request: Request) -> HTMLResponse:
    """Plain-text-style status summary — the local equivalent of the Telegram /status command."""
    async with session_scope() as s:
        faucets_by_status = dict(
            (
                await s.execute(
                    select(Faucet.status, func.count(Faucet.id)).group_by(Faucet.status)
                )
            ).all()
        )
        today_new = (
            await s.execute(
                select(func.count(Project.id)).where(
                    Project.discovered_at >= text("now() - interval '1 day'")
                )
            )
        ).scalar_one()
        recent_claims = list(
            (
                await s.execute(select(Claim).order_by(desc(Claim.attempted_at)).limit(5))
            ).scalars()
        )
        failures = list(
            (
                await s.execute(
                    select(Notification)
                    .where(Notification.type == "FAUCET_FAILED")
                    .order_by(desc(Notification.created_at))
                    .limit(5)
                )
            ).scalars()
        )
        today_gas = (
            await s.execute(
                select(func.coalesce(func.sum(Cost.estimated_usd), 0)).where(
                    Cost.occurred_at >= text("now() - interval '1 day'")
                )
            )
        ).scalar_one()
        cooldowns = list(
            (
                await s.execute(
                    select(Faucet).where(Faucet.status == "COOLDOWN").limit(10)
                )
            ).scalars()
        )
    ctx: dict[str, Any] = {
        "faucets_by_status": faucets_by_status,
        "today_new": today_new,
        "recent_claims": recent_claims,
        "failures": failures,
        "today_gas": float(today_gas or 0),
        "cooldowns": cooldowns,
    }
    return templates.TemplateResponse(request, "status.html", ctx)


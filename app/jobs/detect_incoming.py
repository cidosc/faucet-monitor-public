"""Incoming-value detection job.

Scans every enabled wallet on every enabled chain for value that arrived, stores new
arrivals, and alerts on them. This is the "an airdrop landed in your wallet" signal —
it needs no prior knowledge of which token to watch for.

Read-only. No signing, no sending, no approvals.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.chains.registry import make_adapter
from app.config import settings
from app.db import session_scope
from app.detect.incoming import detect_for_wallet
from app.logging_conf import get_logger
from app.models import Chain, IncomingTransfer, Wallet
from app.notify.dedupe import dispatch_pending, enqueue
from app.notify.templates import incoming_transfer

log = get_logger(__name__)


async def _notify(session: Any, t: IncomingTransfer, chain: Chain, wallet: Wallet) -> bool:
    asset = t.asset_symbol or ("原生代币" if not t.contract_address else t.category)
    msg = incoming_transfer(
        wallet_label=wallet.label,
        wallet_address=wallet.address,
        chain=chain.name,
        is_testnet=chain.is_testnet,
        asset=asset,
        amount=t.amount if t.amount is not None else t.raw_value,
        contract=t.contract_address,
        from_address=t.from_address,
        tx_hash=t.tx_hash,
        block_number=t.block_number,
        occurred_at=t.occurred_at,
        category=t.category,
    )
    return await enqueue(
        session,
        type="INCOMING_TRANSFER",
        dedupe_key=f"incoming:{chain.id}:{t.unique_id}",
        payload={"title": msg.title, "body": msg.body},
        wallet_id=wallet.id,
    )


async def detect_incoming() -> dict[str, Any]:
    scanned = new_total = notified = errors = 0
    per_chain: dict[str, int] = {}
    notes: list[str] = []
    unsupported: dict[str, str] = {}
    transports: dict[str, str] = {}

    async with session_scope() as session:
        wallets = list(
            (await session.execute(select(Wallet).where(Wallet.enabled.is_(True)))).scalars()
        )
        # Only scan chains that can actually produce results. Testnets live on public
        # endpoints that refuse topic-only log scans, so including them would just burn
        # requests and fill the log with known capability gaps.
        wanted = set(settings.detect_chain_list)
        chains = [
            c
            for c in (await session.execute(select(Chain).where(Chain.enabled.is_(True)))).scalars()
            if c.key in wanted
        ]

    if not wallets:
        log.info("detect_incoming_skipped", extra={"reason": "no enabled wallets"})
        return {"items_found": 0, "items_updated": 0, "note": "no wallets configured"}
    if not chains:
        return {
            "items_found": 0,
            "items_updated": 0,
            "note": f"DETECT_INCOMING_CHAINS matched none of the enabled chains ({sorted(wanted)})",
        }

    for chain in chains:
        adapter = make_adapter(chain) if chain.chain_type == "evm" else None
        if adapter is None or not adapter.configured:
            notes.append(f"{chain.key}: rpc_not_configured")
            continue

        for wallet in wallets:
            try:
                async with adapter:
                    async with session_scope() as session:
                        fresh_wallet = (
                            await session.execute(select(Wallet).where(Wallet.id == wallet.id))
                        ).scalar_one()
                        fresh_chain = (
                            await session.execute(select(Chain).where(Chain.id == chain.id))
                        ).scalar_one()
                        summary = await detect_for_wallet(
                            session, fresh_wallet, fresh_chain, adapter
                        )
                scanned += 1
                new_total += summary["new"]
                per_chain[chain.key] = per_chain.get(chain.key, 0) + summary["new"]
                if summary.get("transport"):
                    transports[chain.key] = summary["transport"]
                if summary.get("error"):
                    if summary.get("unsupported"):
                        # Capability gap, not a failure. Surface it once, quietly.
                        if chain.key not in unsupported:
                            unsupported[chain.key] = summary["error"][:100]
                    else:
                        notes.append(f"{chain.key}/{wallet.label}: {summary['error'][:80]}")
            except Exception:  # noqa: BLE001 - isolate per wallet/chain
                log.exception(
                    "detect_incoming_failed",
                    extra={"chain": chain.key, "wallet": wallet.label},
                )
                errors += 1

    # Notify about anything not yet surfaced (including rows written by an earlier run
    # that crashed before notifying — the flag is the source of truth, not this run).
    try:
        async with session_scope() as session:
            pending = list(
                (
                    await session.execute(
                        select(IncomingTransfer, Chain, Wallet)
                        .join(Chain, Chain.id == IncomingTransfer.chain_id)
                        .join(Wallet, Wallet.id == IncomingTransfer.wallet_id)
                        .where(IncomingTransfer.notified.is_(False))
                        .order_by(IncomingTransfer.id)
                        .limit(50)
                    )
                ).all()
            )
            for t, chain, wallet in pending:
                await _notify(session, t, chain, wallet)
                t.notified = True
                notified += 1
            if notified:
                await dispatch_pending(session)
    except Exception:  # noqa: BLE001
        log.exception("detect_incoming_notify_failed")

    summary_out = {
        "items_found": new_total,
        "items_updated": notified,
        "error_count": errors,
        "scanned_pairs": scanned,
        "new_transfers": new_total,
        "notified": notified,
        "per_chain": per_chain,
        "transports": transports,
        "unsupported_chains": unsupported,
        "notes": notes[:10],
    }
    log.info("detect_incoming_complete", extra=summary_out)
    return summary_out


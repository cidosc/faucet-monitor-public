"""Cost job: resolve pending transactions into gas/USD records.

Idempotent: a transaction already having a cost row is skipped, and the receipt data
is keyed off the on-chain values so re-running cannot double-count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.chains.registry import make_adapter
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import Chain, Cost, Transaction
from app.pricing.base import compute_usd, get_price_provider

log = get_logger(__name__)


async def refresh_costs() -> dict[str, Any]:
    processed = errors = 0
    provider = get_price_provider()

    async with session_scope() as session:
        pending = list(
            (
                await session.execute(
                    select(Transaction)
                    .outerjoin(Cost, Cost.transaction_id == Transaction.id)
                    .where(Cost.id.is_(None))
                    .where(Transaction.status.in_(["PENDING", "CONFIRMED"]))
                    .limit(200)
                )
            ).scalars()
        )

    for tx in pending:
        try:
            async with session_scope() as session:
                chain = (
                    await session.execute(select(Chain).where(Chain.id == tx.chain_id))
                ).scalar_one()
                adapter = make_adapter(chain)
                if not adapter.configured:
                    log.info("cost_skipped_no_rpc", extra={"chain": chain.key})
                    continue

                async with adapter:
                    receipt = await adapter.get_transaction_receipt(tx.tx_hash)

                if not receipt.ok:
                    errors += 1
                    continue
                r = receipt.value or {}
                if not r:
                    continue  # not mined yet; retry on the next run

                status = "CONFIRMED" if int(r.get("status", "0x0"), 16) == 1 else "FAILED"
                gas_used = int(r.get("gasUsed", "0x0"), 16)
                eff_price = r.get("effectiveGasPrice")
                gas_price = int(eff_price, 16) if eff_price else None
                if gas_price is None:
                    tx_data = r.get("gasPrice")
                    gas_price = int(tx_data, 16) if tx_data else None

                price = await provider.price_usd(chain.native_symbol)
                usd = compute_usd(gas_used, gas_price, price)

                fresh_tx = (
                    await session.execute(select(Transaction).where(Transaction.id == tx.id))
                ).scalar_one()
                fresh_tx.status = status
                fresh_tx.block_number = int(r.get("blockNumber", "0x0"), 16) or None
                fresh_tx.raw = {"receipt": _shrink(r)}

                session.add(
                    Cost(
                        transaction_id=fresh_tx.id,
                        project_id=fresh_tx.project_id,
                        wallet_id=fresh_tx.wallet_id,
                        chain_id=fresh_tx.chain_id,
                        gas_used=Decimal(gas_used),
                        gas_price_wei=Decimal(gas_price) if gas_price is not None else None,
                        native_symbol=chain.native_symbol,
                        native_price_usd=price,
                        price_source=provider.name,
                        # NULL when the price is unknown, rather than a misleading 0.
                        estimated_usd=usd,
                        occurred_at=fresh_tx.occurred_at or datetime.now(UTC),
                    )
                )
                processed += 1
        except Exception:  # noqa: BLE001
            log.exception("cost_refresh_failed", extra={"tx": tx.tx_hash})
            errors += 1

    summary = {"items_found": len(pending), "items_updated": processed, "error_count": errors}
    log.info("costs_complete", extra=summary)
    return summary


def _shrink(receipt: dict[str, Any]) -> dict[str, Any]:
    keep = ("status", "gasUsed", "effectiveGasPrice", "blockNumber", "from", "to", "logsBloom")
    return {k: v for k, v in receipt.items() if k in keep}


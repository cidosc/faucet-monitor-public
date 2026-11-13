"""Detect value arriving in a monitored wallet.

This is how an airdrop is usually discovered: tokens appear that you never asked for.
No prior knowledge of the token contract is required.

Two transports are available, depending on provider capabilities:

  * Indexed (Alchemy): `alchemy_getAssetTransfers` with pagination.
  * Chunked log scan: `eth_getLogs` filtered to `Transfer(_, to, _)`.

Provider support, block ranges and rate limits vary. Configure scan limits for
an endpoint you are authorized to use; results need not cover the full history.

Both paths are read-only. Nothing here signs, sends, or approves anything.

Idempotency: every transfer carries a stable `unique_id` and the table has
UNIQUE(chain_id, unique_id), so overlapping scan windows can never double-count or
double-notify.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from eth_utils import keccak
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chains.evm import EvmAdapter
from app.claims.abi import decode_string
from app.config import settings
from app.logging_conf import get_logger
from app.models import Chain, IncomingTransfer, Wallet

log = get_logger(__name__)

TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()

# The database stores amounts as NUMERIC(78, 18): 60 integer digits, which covers the
# full uint256 range (max ~1.16e77). Anything larger is not a real amount, so it is
# dropped rather than allowed to abort the scan. This happened for real: junk tokens
# with absurd supplies are sent to addresses constantly, and one such transfer overflowed
# NUMERIC(38, 18) and killed the whole job.
MAX_AMOUNT = Decimal(10) ** 60


def safe_amount(value: Decimal | None) -> Decimal | None:
    """Return the value only if it fits the column; otherwise None (raw is kept in JSON)."""
    if value is None:
        return None
    try:
        if value.is_nan() or value.is_infinite():
            return None
        if abs(value) >= MAX_AMOUNT:
            return None
    except Exception:  # noqa: BLE001
        return None
    return value

# Tokens that are pointless to alert on: wrapped native, and the chain's own stable
# wrappers, which move constantly and signal nothing. Kept small and explicit.
LOW_SIGNAL_SYMBOLS = {"WETH", "WMATIC", "WPOL", "WBNB", "WXPL", "WS", "WBERA"}


@dataclass
class DetectedTransfer:
    unique_id: str
    category: str
    tx_hash: str | None = None
    block_number: int | None = None
    occurred_at: datetime | None = None
    contract_address: str | None = None
    asset_symbol: str | None = None
    asset_name: str | None = None
    raw_value: Decimal | None = None
    decimals: int | None = None
    amount: Decimal | None = None
    from_address: str | None = None
    to_address: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_low_signal(self) -> bool:
        return (self.asset_symbol or "").upper() in LOW_SIGNAL_SYMBOLS


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:  # noqa: BLE001
        return None


def _dec(value: Any) -> int | None:
    try:
        if isinstance(value, str):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError):
        return None


# ─────────────────────────── indexed path ───────────────────────────


async def scan_indexed(adapter: EvmAdapter, address: str, max_count: int) -> tuple[list[DetectedTransfer], str | None]:
    res = await adapter.get_asset_transfers(address, direction="in", max_count=max_count)
    if not res.ok:
        return [], res.error
    out: list[DetectedTransfer] = []
    for t in res.value or []:
        raw_contract = t.get("rawContract") or {}
        dec = _dec(raw_contract.get("decimal"))
        raw_hex = raw_contract.get("value")
        raw_int: int | None = None
        if isinstance(raw_hex, str) and raw_hex.startswith("0x") and raw_hex != "0x":
            try:
                raw_int = int(raw_hex, 16)
            except ValueError:
                raw_int = None
        amount: Decimal | None = None
        if raw_int is not None and dec is not None:
            amount = Decimal(raw_int) / (Decimal(10) ** dec)
        elif t.get("value") is not None:
            try:
                amount = Decimal(str(t["value"]))
            except (InvalidOperation, TypeError):
                amount = None

        uid = t.get("uniqueId") or f"{t.get('hash')}:{t.get('blockNum')}"
        raw_dec = Decimal(raw_int) if raw_int is not None else None
        out.append(
            DetectedTransfer(
                unique_id=str(uid)[:160],
                category=str(t.get("category") or "external"),
                tx_hash=t.get("hash"),
                block_number=_dec(t.get("blockNum")),
                occurred_at=_ts((t.get("metadata") or {}).get("blockTimestamp")),
                contract_address=(raw_contract.get("address") or None),
                asset_symbol=t.get("asset"),
                asset_name=t.get("asset"),
                raw_value=safe_amount(raw_dec),
                decimals=dec,
                amount=safe_amount(amount),
                from_address=t.get("from"),
                to_address=t.get("to"),
                raw={
                    "source": "alchemy_getAssetTransfers",
                    "category": t.get("category"),
                    "tokenId": t.get("tokenId") or t.get("erc721TokenId"),
                    # Preserve the exact value even when it is too large to store
                    # numerically, so nothing is silently lost.
                    "raw_hex": raw_hex,
                    "amount_unclamped": str(amount) if amount is not None else None,
                },
            )
        )
    return out, None


# ─────────────────────────── log-scan path ───────────────────────────


async def scan_logs(
    adapter: EvmAdapter,
    address: str,
    *,
    chunk_blocks: int,
    max_chunks: int,
) -> tuple[list[DetectedTransfer], str | None]:
    head = await adapter.block_number()
    if not head.ok or head.value is None:
        return [], f"block_number_failed: {head.error}"
    latest = int(head.value)
    topic_to = "0x" + "0" * 24 + address.lower().replace("0x", "")

    found: dict[str, DetectedTransfer] = {}
    for i in range(max_chunks):
        hi = latest - i * chunk_blocks
        if hi <= 0:
            break
        lo = max(0, hi - chunk_blocks + 1)
        res = await adapter.get_logs(
            topics=[TRANSFER_TOPIC, None, topic_to], from_block=lo, to_block=hi
        )
        if not res.ok:
            # Report and stop rather than silently returning partial data.
            return list(found.values()), f"get_logs_failed at {lo}-{hi}: {res.error}"
        for lg in res.value or []:
            contract = (lg.get("address") or "").lower()
            idx = lg.get("logIndex") or "0x0"
            txhash = lg.get("transactionHash") or ""
            uid = f"{txhash}:{idx}"
            data = lg.get("data") or "0x"
            raw_int = int(data, 16) if data not in ("0x", "") else 0
            topics = lg.get("topics") or []
            found[uid] = DetectedTransfer(
                unique_id=uid[:160],
                category="erc20",
                tx_hash=txhash,
                block_number=int(lg["blockNumber"], 16) if lg.get("blockNumber") else None,
                contract_address=contract or None,
                raw_value=safe_amount(Decimal(raw_int)),
                from_address=("0x" + topics[1][-40:]) if len(topics) > 1 else None,
                to_address=address,
                raw={"source": "eth_getLogs", "chunk": f"{lo}-{hi}", "raw_hex": data},
            )

    # Resolve symbol/decimals from the chain rather than assuming.
    for t in found.values():
        if not t.contract_address:
            continue
        sym = await adapter.call(t.contract_address, "0x95d89b41")
        dec = await adapter.call(t.contract_address, "0x313ce567")
        if sym.ok and sym.value:
            t.asset_symbol = decode_string(sym.value)
            t.asset_name = t.asset_symbol
        d = _dec(dec.value) if dec.ok else None
        t.decimals = d if d is not None else 18
        if t.raw_value is not None:
            t.amount = safe_amount(t.raw_value / (Decimal(10) ** t.decimals))
    return list(found.values()), None


# ─────────────────────────── persistence ───────────────────────────


UNSUPPORTED_MARKERS = (
    "unsupported method",
    "asset_transfers_unsupported",
    "please specify an address",
    "free tier plan",
    "limited to a",
    "archive requests require",
)


def is_unsupported(error: str | None) -> bool:
    """Distinguish "this provider cannot do that" from "something went wrong".

    A chain whose provider refuses topic-only log scans is not an error condition — it
    is a known capability gap, and reporting it as an error every 30 minutes would be
    noise that hides real failures.
    """
    if not error:
        return False
    e = error.lower()
    return any(m in e for m in UNSUPPORTED_MARKERS)


async def detect_for_wallet(
    session: AsyncSession, wallet: Wallet, chain: Chain, adapter: EvmAdapter
) -> dict[str, Any]:
    """Scan one wallet on one chain, persist new arrivals. Returns a summary."""
    result: dict[str, Any] = {
        "chain": chain.key, "wallet": wallet.label,
        "found": 0, "new": 0, "error": None, "transport": None, "unsupported": False,
    }

    transfers: list[DetectedTransfer] = []
    err: str | None = None

    # Prefer indexed transfers when advertised; fall back if the method is unsupported.
    if adapter.supports_asset_transfers:
        transfers, err = await scan_indexed(
            adapter, wallet.address, settings.scan_max_transfers
        )
        result["transport"] = "alchemy_indexed"
        if err and is_unsupported(err):
            log.info(
                "indexed_transfers_unsupported_falling_back",
                extra={"chain": chain.key, "error": err[:80]},
            )
            transfers, err = [], None
            result["transport"] = None

    if result["transport"] is None:
        transfers, err = await scan_logs(
            adapter,
            wallet.address,
            chunk_blocks=settings.scan_chunk_blocks,
            max_chunks=settings.scan_max_chunks,
        )
        result["transport"] = "log_scan"

    result["found"] = len(transfers)
    if err:
        if is_unsupported(err):
            # Capability gap, not a failure. Record it distinctly and carry on.
            result["unsupported"] = True
            result["error"] = f"unsupported: {err[:120]}"
            log.info(
                "incoming_scan_unsupported",
                extra={"chain": chain.key, "wallet": wallet.label, "error": err[:80]},
            )
            return result
        result["error"] = err
        log.warning(
            "incoming_scan_partial",
            extra={"chain": chain.key, "wallet": wallet.label, "error": err,
                   "partial": len(transfers)},
        )

    new_count = 0
    for t in transfers:
        exists = (
            await session.execute(
                select(IncomingTransfer.id).where(
                    IncomingTransfer.chain_id == chain.id,
                    IncomingTransfer.unique_id == t.unique_id,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        session.add(
            IncomingTransfer(
                chain_id=chain.id,
                wallet_id=wallet.id,
                unique_id=t.unique_id,
                tx_hash=t.tx_hash,
                block_number=t.block_number,
                occurred_at=t.occurred_at,
                category=t.category,
                contract_address=t.contract_address,
                asset_symbol=t.asset_symbol,
                asset_name=t.asset_name,
                raw_value=t.raw_value,
                decimals=t.decimals,
                amount=t.amount,
                from_address=t.from_address,
                to_address=t.to_address,
                raw=t.raw,
            )
        )
        new_count += 1
    await session.flush()
    result["new"] = new_count
    return result


def address_is_valid_for_chain(address: str, chain_type: str = "evm") -> bool:
    from app.wallets import is_valid_address

    return is_valid_address(address, chain_type)


__all__ = [
    "DetectedTransfer",
    "LOW_SIGNAL_SYMBOLS",
    "MAX_AMOUNT",
    "TRANSFER_TOPIC",
    "detect_for_wallet",
    "is_unsupported",
    "safe_amount",
    "scan_indexed",
    "scan_logs",
]


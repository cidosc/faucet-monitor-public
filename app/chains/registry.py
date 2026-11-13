"""Chain registry: loads config/chains.yaml, syncs into the DB, hands out adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chains.evm import EvmAdapter
from app.config import settings
from app.logging_conf import get_logger
from app.models import Chain

log = get_logger(__name__)


def load_chain_defs(path: str | None = None) -> list[dict[str, Any]]:
    p = Path(path or settings.chains_config_path)
    if not p.exists():
        log.warning("chains_config_missing", extra={"path": str(p)})
        return []
    data = yaml.safe_load(p.read_text()) or {}
    return list(data.get("chains") or [])


async def sync_chains(session: AsyncSession, defs: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """Upsert chain definitions. Returns counts."""
    defs = defs if defs is not None else load_chain_defs()
    created = updated = 0

    for d in defs:
        key = d.get("key")
        if not key:
            continue
        row = (await session.execute(select(Chain).where(Chain.key == key))).scalar_one_or_none()
        fields = {
            "name": d.get("name", key),
            "chain_type": d.get("chain_type", "evm"),
            "chain_id": d.get("chain_id"),
            "native_symbol": d.get("native_symbol", "ETH"),
            "rpc_url_env": d.get("rpc_url_env") or settings.rpc_env_name(key),
            "explorer_url": d.get("explorer_url"),
            "is_testnet": bool(d.get("is_testnet", True)),
            "enabled": bool(d.get("enabled", True)),
        }
        if row is None:
            session.add(Chain(key=key, **fields))
            created += 1
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            updated += 1

    await session.flush()
    log.info("chains_synced", extra={"created": created, "updated": updated})
    return {"created": created, "updated": updated}


def make_adapter(chain: Chain) -> EvmAdapter:
    """Build the right adapter for a chain row. Non-EVM chains are not implemented in v1."""
    if chain.chain_type != "evm":
        raise NotImplementedError(
            f"chain_type '{chain.chain_type}' is not implemented in v1 "
            "(the interface is reserved; see app/chains/base.py)"
        )
    return EvmAdapter(
        key=chain.key,
        rpc_url=settings.rpc_url(chain.key),
        chain_id=chain.chain_id,
        native_symbol=chain.native_symbol,
    )


async def verify_all(session: AsyncSession) -> list[dict[str, Any]]:
    """Probe every enabled chain with eth_chainId. Mismatches are reported, never ignored."""
    chains = list((await session.execute(select(Chain).where(Chain.enabled.is_(True)))).scalars())
    out: list[dict[str, Any]] = []
    for chain in chains:
        if chain.chain_type != "evm":
            out.append({"key": chain.key, "ok": False, "error": "not_implemented"})
            continue
        adapter = make_adapter(chain)
        try:
            async with adapter:
                res = await adapter.verify()
            out.append({
                "key": chain.key,
                "ok": res.ok,
                "chain_id": res.value if res.ok else None,
                "error": res.error,
                "rpc_env": chain.rpc_url_env,
                "configured": adapter.configured,
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"key": chain.key, "ok": False, "error": repr(exc)})
    return out


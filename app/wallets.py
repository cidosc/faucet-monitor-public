"""Wallet management.

Wallet ADDRESSES are public data. No private key, mnemonic, keystore or seed is ever
stored, accepted, or read by this module — `wallets` has no such column and never will.

Two supported ways to add a wallet:
  1. config/wallets.yaml  (versionable, reviewed, loaded by the discover job)
  2. `python -m app.cli wallet add --label X --address 0x... --chain eth-sepolia`

`auto_faucet` defaults to FALSE for every wallet, including ones added here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_conf import get_logger
from app.models import Chain, Wallet

log = get_logger(__name__)

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_valid_address(address: str, chain_type: str = "evm") -> bool:
    if chain_type == "evm":
        return bool(_EVM_RE.match(address.strip()))
    # Non-EVM address validation is intentionally not implemented in v1 rather than
    # guessed — an unrecognised format is accepted only if clearly non-empty.
    return bool(address and len(address.strip()) >= 26)


def wallets_config_path() -> Path:
    from app.config import settings

    return Path(settings.wallets_config_path)


def load_wallet_defs(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or wallets_config_path()
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        log.error("wallets_config_invalid", extra={"path": str(p), "error": str(exc)})
        return []
    return list(data.get("wallets") or [])


async def sync_wallets_from_config(session: AsyncSession, defs: list[dict[str, Any]] | None = None) -> dict[str, int]:
    defs = defs if defs is not None else load_wallet_defs()
    created = updated = skipped = 0

    chains = {c.key: c for c in (await session.execute(select(Chain))).scalars()}

    for d in defs:
        label = (d.get("name") or d.get("label") or "").strip()
        address = (d.get("address") or "").strip()
        if not label or not address:
            continue

        chain_key = d.get("chain")
        chain = chains.get(chain_key) if chain_key else None
        if chain_key and chain is None:
            log.warning("wallet_unknown_chain", extra={"label": label, "chain": chain_key})

        chain_type = chain.chain_type if chain else "evm"
        if not is_valid_address(address, chain_type):
            log.warning(
                "wallet_invalid_address_skipped",
                extra={"label": label, "chain_type": chain_type},
            )
            skipped += 1
            continue

        row = (await session.execute(select(Wallet).where(Wallet.label == label))).scalar_one_or_none()
        if row is None:
            row = Wallet(label=label, address=address)
            session.add(row)
            created += 1
        else:
            updated += 1

        row.address = address
        row.chain_id = chain.id if chain else None
        row.notes = d.get("notes")
        # Both flags are opt-in. `is_test_wallet` is what permits automation, so it
        # defaults to False: a wallet in this file is a real wallet unless it says
        # otherwise.
        row.auto_faucet = bool(d.get("auto_faucet", False))
        row.is_test_wallet = bool(d.get("is_test_wallet", False))
        row.enabled = bool(d.get("enabled", True))
        if row.auto_faucet and not row.is_test_wallet:
            log.warning(
                "auto_faucet_forced_off_for_non_test_wallet",
                extra={"label": label},
            )
            row.auto_faucet = False

    await session.flush()
    if created or updated or skipped:
        log.info("wallets_synced", extra={"created": created, "updated": updated, "skipped": skipped})
    return {"created": created, "updated": updated, "skipped": skipped}


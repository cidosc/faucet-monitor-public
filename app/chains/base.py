"""Chain adapter protocol.

EVM is implemented in v1. A non-EVM adapter (Solana) only has to satisfy this
protocol — nothing else in the codebase is EVM-aware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


@dataclass
class ChainProbeResult:
    """Outcome of a read-only chain query."""

    ok: bool
    value: Any = None
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenBalance:
    raw: int
    decimals: int
    symbol: str | None = None

    @property
    def human(self) -> Decimal:
        return Decimal(self.raw) / (Decimal(10) ** self.decimals)


@runtime_checkable
class ChainAdapter(Protocol):
    """Read-only chain access. No adapter may sign or send anything."""

    key: str
    chain_type: str

    async def chain_id(self) -> ChainProbeResult: ...

    async def block_number(self) -> ChainProbeResult: ...

    async def native_balance(self, address: str) -> ChainProbeResult: ...

    async def transaction_count(self, address: str) -> ChainProbeResult: ...

    async def get_transaction_receipt(self, tx_hash: str) -> ChainProbeResult: ...

    async def call(self, to: str, data: str, block: str = "latest") -> ChainProbeResult: ...

    async def get_logs(
        self,
        *,
        address: str | None = None,
        topics: list[str | None] | None = None,
        from_block: int | str = 0,
        to_block: int | str = "latest",
    ) -> ChainProbeResult: ...

    async def erc20_balance(self, token: str, holder: str) -> ChainProbeResult: ...

    async def erc721_balance(self, token: str, holder: str) -> ChainProbeResult: ...


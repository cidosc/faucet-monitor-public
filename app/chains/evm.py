"""Generic EVM JSON-RPC adapter.

Implements exactly the methods called for in the requirements:
    eth_chainId, eth_getBalance, eth_blockNumber, eth_getTransactionReceipt,
    eth_call, eth_getLogs   (+ eth_getTransactionCount for transaction-count checks)

The RPC endpoint is ALWAYS resolved from the environment by `rpc_url_env` — never
hardcoded. Read-only by construction: the only JSON-RPC methods exposed are `eth_*`
queries. There is no method here that can sign, send, or mutate chain state.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx

from app.chains.base import ChainProbeResult, TokenBalance
from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

_ids = itertools.count(1)

# Function selectors (first 4 bytes of keccak256 of the signature).
SEL_BALANCE_OF = "0x70a08231"       # balanceOf(address)
SEL_DECIMALS = "0x313ce567"         # decimals()
SEL_SYMBOL = "0x95d89b41"           # symbol()
SEL_TOTAL_SUPPLY = "0x18160ddd"     # totalSupply()


def _encode_address(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    if len(a) != 40:
        raise ValueError(f"not a 20-byte address: {addr}")
    return a.rjust(64, "0")


def _decode_uint(hexstr: str) -> int:
    if not hexstr or hexstr == "0x":
        return 0
    return int(hexstr, 16)


def _decode_string(hexstr: str) -> str | None:
    """Decode a solidity `string` return value."""
    try:
        raw = bytes.fromhex(hexstr[2:])
        if len(raw) < 64:
            return None
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 > len(raw):
            return None
        length = int.from_bytes(raw[offset : offset + 32], "big")
        data = raw[offset + 32 : offset + 32 + length]
        return data.decode("utf-8", errors="replace").strip("\x00").strip()
    except Exception:  # noqa: BLE001
        return None


class EvmAdapter:
    chain_type = "evm"

    def __init__(self, key: str, rpc_url: str | None, chain_id: int | None = None,
                 native_symbol: str = "ETH") -> None:
        self.key = key
        self._rpc_url = rpc_url
        self.expected_chain_id = chain_id
        self.native_symbol = native_symbol
        self._client: httpx.AsyncClient | None = None

    # ── lifecycle ──
    async def __aenter__(self) -> EvmAdapter:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.rpc_timeout_seconds),
            headers={"Content-Type": "application/json", "User-Agent": settings.http_user_agent},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def configured(self) -> bool:
        return bool(self._rpc_url)

    # ── transport ──
    async def _rpc(self, method: str, params: list[Any] | None = None) -> ChainProbeResult:
        if not self._rpc_url:
            return ChainProbeResult(False, error=f"rpc_not_configured ({settings.rpc_env_name(self.key)})")
        if self._client is None:
            raise RuntimeError("EvmAdapter must be used as an async context manager")

        payload = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or []}
        last_error = None
        for attempt in range(1, settings.rpc_max_retries + 1):
            try:
                resp = await self._client.post(self._rpc_url, json=payload)
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    return ChainProbeResult(
                        False, error=f"rpc_error {err.get('code')}: {err.get('message')}",
                        evidence={"method": method},
                    )
                return ChainProbeResult(
                    True, value=data.get("result"), evidence={"method": method, "attempts": attempt}
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= settings.rpc_max_retries:
                    break
                log.info("rpc_retry", extra={"chain": self.key, "method": method,
                                             "attempt": attempt, "error": last_error})

        return ChainProbeResult(False, error=last_error, evidence={"method": method})

    # ── read-only API ──
    async def chain_id(self) -> ChainProbeResult:
        res = await self._rpc("eth_chainId")
        if res.ok and res.value is not None:
            res.value = int(res.value, 16)
            res.evidence["matches_expected"] = (
                self.expected_chain_id is None or res.value == self.expected_chain_id
            )
        return res

    async def block_number(self) -> ChainProbeResult:
        res = await self._rpc("eth_blockNumber")
        if res.ok and res.value is not None:
            res.value = int(res.value, 16)
        return res

    async def native_balance(self, address: str) -> ChainProbeResult:
        res = await self._rpc("eth_getBalance", [address, "latest"])
        if res.ok and res.value is not None:
            res.value = int(res.value, 16)
            res.evidence["symbol"] = self.native_symbol
        return res

    async def transaction_count(self, address: str) -> ChainProbeResult:
        """Nonce = number of transactions SENT by this address. Publicly verifiable."""
        res = await self._rpc("eth_getTransactionCount", [address, "latest"])
        if res.ok and res.value is not None:
            res.value = int(res.value, 16)
        return res

    async def transaction_count_pending(self, address: str) -> ChainProbeResult:
        """Nonce including pending transactions.

        必须用 "pending" 而不是 "latest" —— 否则连续两笔交易会拿到同一个 nonce，
        第二笔被节点拒绝（或更糟：覆盖第一笔）。
        """
        res = await self._rpc("eth_getTransactionCount", [address, "pending"])
        if res.ok and res.value is not None:
            res.value = int(res.value, 16)
        return res

    # ── 写入路径 ──
    # 下面两个方法是本适配器里唯一会改变链上状态的东西。它们只接受已经签好的
    # 原始交易 —— 私钥不经过这里，签名由 app/autoclaim/executor.py 在内存里完成。

    async def send_raw_transaction(self, raw_hex: str | bytes) -> ChainProbeResult:
        """广播一笔已签名的交易。调用方负责幂等与成本控制。"""
        if isinstance(raw_hex, (bytes, bytearray)):
            raw_hex = "0x" + bytes(raw_hex).hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        return await self._rpc("eth_sendRawTransaction", [raw_hex])

    async def wait_for_receipt(
        self, tx_hash: str, *, timeout: float = 120.0, interval: float = 3.0
    ) -> dict[str, Any] | None:
        """轮询到交易被打包。超时返回 None（交易仍可能之后上链）。"""
        import asyncio
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = await self.get_transaction_receipt(tx_hash)
            if res.ok and isinstance(res.value, dict) and res.value:
                return res.value
            await asyncio.sleep(interval)
        return None

    async def get_transaction_receipt(self, tx_hash: str) -> ChainProbeResult:
        return await self._rpc("eth_getTransactionReceipt", [tx_hash])

    async def call(
        self, to: str, data: str, block: str = "latest", sender: str | None = None
    ) -> ChainProbeResult:
        """`eth_call`. With `sender` set, the node executes as if that account sent it —
        this is how a claim is simulated without signing or spending anything."""
        tx: dict[str, Any] = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        return await self._rpc("eth_call", [tx, block])

    async def estimate_gas(
        self, to: str, data: str, sender: str | None = None, value: int | None = None
    ) -> ChainProbeResult:
        """`eth_estimateGas`. Read-only: nothing is broadcast."""
        tx: dict[str, Any] = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        if value:
            tx["value"] = hex(value)
        res = await self._rpc("eth_estimateGas", [tx])
        if res.ok and res.value is not None:
            res.value = int(res.value, 16)
        return res

    async def gas_price(self) -> ChainProbeResult:
        res = await self._rpc("eth_gasPrice")
        if res.ok and res.value is not None:
            res.value = int(res.value, 16)
        return res

    # ── indexed transfer history (Alchemy only) ──
    @property
    def supports_asset_transfers(self) -> bool:
        """Infer indexed API support from the endpoint; callers must handle rejection."""
        return bool(self._rpc_url and "alchemy.com" in self._rpc_url)

    async def get_asset_transfers(
        self,
        address: str,
        *,
        direction: str = "in",
        categories: list[str] | None = None,
        max_count: int = 100,
        from_block: str = "0x0",
        order: str = "desc",
    ) -> ChainProbeResult:
        """Incoming/outgoing transfers for an address. Read-only indexed lookup.

        `direction='in'` means "everything ever sent TO this address", which is exactly
        how an unannounced airdrop shows up.
        """
        if not self.supports_asset_transfers:
            return ChainProbeResult(
                False, error="asset_transfers_unsupported (not an Alchemy endpoint)"
            )
        params: dict[str, Any] = {
            "fromBlock": from_block,
            "toBlock": "latest",
            "category": categories or ["external", "erc20", "erc721", "erc1155"],
            "withMetadata": True,
            "excludeZeroValue": True,
            "maxCount": hex(max_count),
            "order": order,
        }
        if direction == "in":
            params["toAddress"] = address
        else:
            params["fromAddress"] = address

        res = await self._rpc("alchemy_getAssetTransfers", [params])
        if res.ok and isinstance(res.value, dict):
            res.value = res.value.get("transfers") or []
        return res

    async def get_logs(
        self,
        *,
        address: str | None = None,
        topics: list[str | None] | None = None,
        from_block: int | str = 0,
        to_block: int | str = "latest",
    ) -> ChainProbeResult:
        def _hex(v: int | str) -> str:
            return v if isinstance(v, str) else hex(v)

        params: dict[str, Any] = {"fromBlock": _hex(from_block), "toBlock": _hex(to_block)}
        if address:
            params["address"] = address
        if topics:
            params["topics"] = topics
        return await self._rpc("eth_getLogs", [params])

    async def erc20_balance(self, token: str, holder: str) -> ChainProbeResult:
        res = await self.call(token, SEL_BALANCE_OF + _encode_address(holder))
        if not res.ok:
            return res
        raw = _decode_uint(res.value or "0x0")
        dec = await self.call(token, SEL_DECIMALS)
        decimals = _decode_uint(dec.value or "0x12") if dec.ok else 18
        sym = await self.call(token, SEL_SYMBOL)
        symbol = _decode_string(sym.value) if sym.ok and sym.value else None
        res.value = TokenBalance(raw=raw, decimals=decimals, symbol=symbol)
        res.evidence.update({"token": token, "holder": holder, "decimals": decimals})
        return res

    async def erc721_balance(self, token: str, holder: str) -> ChainProbeResult:
        res = await self.call(token, SEL_BALANCE_OF + _encode_address(holder))
        if not res.ok:
            return res
        res.value = _decode_uint(res.value or "0x0")
        res.evidence.update({"token": token, "holder": holder, "standard": "erc721"})
        return res

    async def verify(self) -> ChainProbeResult:
        """Startup self-check: a wrong/mismatched endpoint must be caught, not trusted."""
        if not self.configured:
            return ChainProbeResult(
                False, error=f"rpc_not_configured ({settings.rpc_env_name(self.key)})"
            )
        res = await self.chain_id()
        if not res.ok:
            return res
        if self.expected_chain_id is not None and res.value != self.expected_chain_id:
            return ChainProbeResult(
                False,
                value=res.value,
                error=f"chain_id_mismatch: rpc returned {res.value}, config says {self.expected_chain_id}",
                evidence=res.evidence,
            )
        return res


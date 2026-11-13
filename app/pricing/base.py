"""Token price providers.

Default is deliberately ZERO — an unknown price must not look like a confident $0.00
in the other direction either, so `estimated_usd` is left NULL when the price is
unknown, and the dashboard renders "—".

Testnet gas has no real market value. This only matters if mainnet activity is tracked.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from decimal import Decimal

import httpx

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)


class PriceProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def price_usd(self, symbol: str) -> Decimal | None:
        """Return USD price, or None when unknown. Never guess."""


class StaticPriceProvider(PriceProvider):
    """Prices from STATIC_PRICES_JSON, falling back to config/prices.yaml."""

    name = "static"

    def __init__(self) -> None:
        self._prices: dict[str, Decimal] = {}
        self._load()

    def _load(self) -> None:
        raw = settings.static_prices
        for k, v in raw.items():
            try:
                self._prices[k.upper()] = Decimal(str(v))
            except Exception:  # noqa: BLE001
                continue
        if not self._prices:
            from pathlib import Path

            import yaml

            p = Path("config/prices.yaml")
            if p.exists():
                try:
                    data = yaml.safe_load(p.read_text()) or {}
                    for k, v in (data.get("prices") or {}).items():
                        self._prices[str(k).upper()] = Decimal(str(v))
                except Exception:  # noqa: BLE001
                    log.warning("prices_yaml_unreadable")

    async def price_usd(self, symbol: str) -> Decimal | None:
        price = self._prices.get(symbol.upper())
        if price is None:
            return None
        # A configured 0 means "we do not assert a value for this token".
        return price if price > 0 else None


class CoinGeckoPriceProvider(PriceProvider):
    """Optional live prices. Requires outbound access to api.coingecko.com."""

    name = "coingecko"

    IDS = {
        "ETH": "ethereum", "WETH": "weth", "POL": "polygon-ecosystem-token",
        "MATIC": "matic-network", "BNB": "binancecoin", "AVAX": "avalanche-2",
        "SOL": "solana", "FTM": "fantom", "CELO": "celo", "USDC": "usd-coin",
        "USDT": "tether", "DAI": "dai",
    }

    def __init__(self) -> None:
        self._cache: dict[str, Decimal] = {}

    async def price_usd(self, symbol: str) -> Decimal | None:
        sym = symbol.upper()
        if sym in self._cache:
            return self._cache[sym]
        coin_id = self.IDS.get(sym)
        if coin_id is None:
            return None

        headers = {"User-Agent": settings.http_user_agent}
        if settings.coingecko_api_key:
            headers["x-cg-demo-api-key"] = settings.coingecko_api_key
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{settings.coingecko_base_url}/simple/price",
                    params={"ids": coin_id, "vs_currencies": "usd"},
                    headers=headers,
                )
            data = resp.json()
            price = Decimal(str(data[coin_id]["usd"]))
            self._cache[sym] = price
            return price
        except Exception as exc:  # noqa: BLE001
            log.warning("coingecko_price_failed", extra={"symbol": sym, "error": str(exc)})
            return None


_provider: PriceProvider | None = None


def get_price_provider() -> PriceProvider:
    global _provider
    if _provider is None:
        _provider = (
            CoinGeckoPriceProvider() if settings.price_provider == "coingecko"
            else StaticPriceProvider()
        )
        log.info("price_provider_selected", extra={"provider": _provider.name})
    return _provider


def compute_usd(gas_used: int | None, gas_price_wei: int | None, price: Decimal | None) -> Decimal | None:
    """cost = gas_used * gas_price_wei (wei) -> native -> USD. None if any input is unknown."""
    if gas_used is None or gas_price_wei is None or price is None:
        return None
    native = (Decimal(gas_used) * Decimal(gas_price_wei)) / Decimal(10**18)
    return (native * price).quantize(Decimal("0.000001"))


def dump_static_prices() -> str:
    return json.dumps({k: str(v) for k, v in StaticPriceProvider()._prices.items()})


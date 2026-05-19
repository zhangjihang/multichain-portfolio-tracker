"""CoinGecko price provider."""

import logging
import time
from decimal import Decimal

import httpx

from ..base import PriceProvider

# Common token mappings: symbol -> coingecko id
SYMBOL_TO_COINGECKO_ID = {
    "ETH": "ethereum",
    "WETH": "ethereum",
    "BTC": "bitcoin",
    "WBTC": "wrapped-bitcoin",
    "USDT": "tether",
    "USDC": "usd-coin",
    "DAI": "dai",
    "SOL": "solana",
    "BNB": "binancecoin",
    "MATIC": "matic-network",
    "AVAX": "avalanche-2",
    "ARB": "arbitrum",
    "OP": "optimism",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "AAVE": "aave",
}

# EVM token addresses to coingecko id (mainnet)
ADDRESS_TO_COINGECKO_ID = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "ethereum",  # WETH
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "wrapped-bitcoin",  # WBTC
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "tether",  # USDT
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "usd-coin",  # USDC
    "0x6b175474e89094c44da98b954eedeac495271d0f": "dai",  # DAI
}

# Chain ID to native token coingecko id
CHAIN_NATIVE_COINGECKO = {
    1: "ethereum",      # Ethereum
    10: "ethereum",     # Optimism (ETH)
    56: "binancecoin",  # BSC (BNB)
    137: "matic-network",  # Polygon (MATIC)
    8453: "ethereum",   # Base (ETH)
    42161: "ethereum",  # Arbitrum (ETH)
    43114: "avalanche-2",  # Avalanche (AVAX)
}


class CoinGeckoProvider(PriceProvider):
    """CoinGecko price provider with caching."""

    BASE_URL = "https://api.coingecko.com/api/v3"

    def __init__(self, cache_ttl: int = 300):
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[Decimal, float]] = {}
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _get_cached(self, key: str) -> Decimal | None:
        if key in self._cache:
            price, timestamp = self._cache[key]
            if time.time() - timestamp < self.cache_ttl:
                return price
        return None

    def _set_cache(self, key: str, price: Decimal) -> None:
        self._cache[key] = (price, time.time())

    def _resolve_coingecko_id(self, asset_id: str) -> str | None:
        """Resolve asset_id to CoinGecko ID."""
        # Handle different asset_id formats
        if asset_id.startswith("eip155:"):
            # EVM token: eip155:1:0x... or eip155:56:native
            parts = asset_id.split(":")
            if len(parts) >= 3:
                try:
                    chain_id = int(parts[1])
                except ValueError:
                    chain_id = 1
                address = parts[2].lower()
                if address == "native":
                    return CHAIN_NATIVE_COINGECKO.get(chain_id, "ethereum")
                return ADDRESS_TO_COINGECKO_ID.get(address)
        elif asset_id.startswith("solana:"):
            parts = asset_id.split(":")
            if len(parts) >= 2 and parts[1] == "native":
                return "solana"
        elif asset_id.startswith("exchange:"):
            # exchange:binance:BTC
            parts = asset_id.split(":")
            if len(parts) >= 3:
                symbol = parts[2].upper()
                return SYMBOL_TO_COINGECKO_ID.get(symbol)

        # Try direct symbol lookup
        return SYMBOL_TO_COINGECKO_ID.get(asset_id.upper())

    async def get_price(self, asset_id: str) -> Decimal:
        """Get current USD price for an asset."""
        cached = self._get_cached(asset_id)
        if cached is not None:
            return cached

        coingecko_id = self._resolve_coingecko_id(asset_id)
        if not coingecko_id:
            return Decimal("0")

        try:
            resp = await self._client.get(
                f"{self.BASE_URL}/simple/price",
                params={"ids": coingecko_id, "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            data = resp.json()

            if coingecko_id in data and "usd" in data[coingecko_id]:
                price = Decimal(str(data[coingecko_id]["usd"]))
                self._set_cache(asset_id, price)
                return price
        except Exception:
            logging.exception("Failed to fetch price for %s from CoinGecko", asset_id)

        return Decimal("0")

    async def get_prices(self, asset_ids: list[str]) -> dict[str, Decimal]:
        """Get current USD prices for multiple assets."""
        result: dict[str, Decimal] = {}
        uncached: list[tuple[str, str]] = []  # (asset_id, coingecko_id)

        for asset_id in asset_ids:
            cached = self._get_cached(asset_id)
            if cached is not None:
                result[asset_id] = cached
            else:
                cg_id = self._resolve_coingecko_id(asset_id)
                if cg_id:
                    uncached.append((asset_id, cg_id))
                else:
                    result[asset_id] = Decimal("0")

        if uncached:
            cg_ids = list(set(cg_id for _, cg_id in uncached))
            try:
                resp = await self._client.get(
                    f"{self.BASE_URL}/simple/price",
                    params={"ids": ",".join(cg_ids), "vs_currencies": "usd"},
                )
                resp.raise_for_status()
                data = resp.json()

                for asset_id, cg_id in uncached:
                    if cg_id in data and "usd" in data[cg_id]:
                        price = Decimal(str(data[cg_id]["usd"]))
                        self._set_cache(asset_id, price)
                        result[asset_id] = price
                    else:
                        result[asset_id] = Decimal("0")
            except Exception:
                logging.exception("Failed to fetch batch prices from CoinGecko for %s", cg_ids)
                for asset_id, _ in uncached:
                    result[asset_id] = Decimal("0")

        return result

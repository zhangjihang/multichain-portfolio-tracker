"""DeBank API adapter for comprehensive EVM DeFi portfolio tracking.

DeBank provides aggregated portfolio data across 56+ EVM chains and hundreds of DeFi protocols.
API Docs: https://docs.cloud.debank.com/en/readme/api-pro-reference
"""

import logging
import os
from decimal import Decimal

import httpx

from ..models import Asset, Liability, Position, PositionType

logger = logging.getLogger(__name__)


# 代币归类映射 (衍生代币 -> 基础代币)
TOKEN_CONVERT = {
    # ETH 系列
    'WETH': 'ETH', 'wstETH': 'ETH', 'stETH': 'ETH', 'rETH': 'ETH',
    'cbETH': 'ETH', 'rsETH': 'ETH', 'ezETH': 'ETH', 'weETH': 'ETH',
    'oETH': 'ETH', 'sfrxETH': 'ETH', 'frxETH': 'ETH', 'mETH': 'ETH',
    'swETH': 'ETH', 'ETHx': 'ETH', 'BETH': 'ETH', 'ankrETH': 'ETH',
    # BTC 系列
    'WBTC': 'BTC', 'BTCB': 'BTC', 'tBTC': 'BTC', 'cbBTC': 'BTC',
    'sBTC': 'BTC', 'renBTC': 'BTC', 'HBTC': 'BTC',
    # SOL 系列
    'wSOL': 'SOL', 'mSOL': 'SOL', 'stSOL': 'SOL', 'jitoSOL': 'SOL',
    'bSOL': 'SOL', 'INF': 'SOL', 'hSOL': 'SOL', 'JitoSOL': 'SOL',
    # 稳定币 -> USD
    'USDC': 'USD', 'USDT': 'USD', 'DAI': 'USD', 'USDT0': 'USD',
    'USDC.e': 'USD', 'BUSD': 'USD', 'USD1': 'USD', 'sUSD': 'USD',
    'FRAX': 'USD', 'LUSD': 'USD', 'crvUSD': 'USD', 'GHO': 'USD',
    'lisUSD': 'USD', 'pre-iUSDT': 'USD', 'iUSDT': 'USD', 'TUSD': 'USD',
    'USDP': 'USD', 'GUSD': 'USD', 'sDAI': 'USD', 'PYUSD': 'USD',
    'CASH': 'USD', 'FDUSD': 'USD', 'USDE': 'USD', 'eUSD': 'USD',
    'USDG': 'USD',
}


class DeBankAdapter:
    """DeBank API adapter for EVM DeFi portfolio data."""

    BASE_URL = "https://pro-openapi.debank.com/v1"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("DEBANK_API_KEY", "")
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"AccessKey": self.api_key},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_units_balance(self) -> dict:
        """Get DeBank API units balance and today's usage.

        Returns: {'balance': int, 'today_usage': int}
        """
        resp = await self._client.get(f"{self.BASE_URL}/account/units")
        resp.raise_for_status()
        data = resp.json()
        balance = data.get("balance", 0)
        stats = data.get("stats", [])
        today_usage = stats[0].get("usage", 0) if stats else 0
        return {"balance": balance, "today_usage": today_usage}

    async def get_total_balance(self, address: str) -> Decimal:
        """Get total USD balance from DeBank."""
        resp = await self._client.get(
            f"{self.BASE_URL}/user/total_balance",
            params={"id": address.lower()},
        )
        resp.raise_for_status()
        return Decimal(str(resp.json().get("total_usd_value", 0)))

    async def get_portfolio(self, address: str) -> dict:
        """Get aggregated portfolio with tokens merged by base asset.

        Returns:
            {
                "assets": {"ETH": {"quantity": Decimal, "value": Decimal, "price": Decimal}, ...},
                "debts": {"USD": {"quantity": Decimal, "value": Decimal, "price": Decimal}, ...},
                "total_assets": Decimal,
                "total_debts": Decimal,
                "net_worth": Decimal,
            }
        """
        # Track value and price for each base token
        assets: dict[str, dict] = {}  # {symbol: {"value": Decimal, "price": Decimal}}
        debts: dict[str, dict] = {}

        def add_token(target: dict, symbol: str, amount: float, price: float):
            """Add token to target dict, merging by base symbol."""
            value = Decimal(str(amount * price))
            if value < 1:
                return

            base = TOKEN_CONVERT.get(symbol, symbol)
            price_dec = Decimal(str(price)) if price else Decimal("0")

            if base not in target:
                target[base] = {"value": Decimal("0"), "price": price_dec}
            target[base]["value"] += value
            # Keep the price of the base token (not derivatives)
            if symbol == base and price_dec > 0:
                target[base]["price"] = price_dec

        # 1. 钱包代币 (只统计 is_wallet=True)
        resp = await self._client.get(
            f"{self.BASE_URL}/user/all_token_list",
            params={"id": address.lower()},
        )
        resp.raise_for_status()

        for t in resp.json():
            if not t.get("is_wallet"):
                continue
            add_token(assets, t.get("symbol", "?"), t.get("amount", 0), t.get("price", 0))

        # 2. DeFi 协议持仓
        resp = await self._client.get(
            f"{self.BASE_URL}/user/all_complex_protocol_list",
            params={"id": address.lower()},
        )
        resp.raise_for_status()

        defi_positions = []  # 保留协议明细

        for protocol in resp.json():
            protocol_name = protocol.get("name", "Unknown")
            protocol_chain = protocol.get("chain", "")

            for item in protocol.get("portfolio_item_list", []):
                detail = item.get("detail", {})
                pos_name = item.get("name", "")

                supply_tokens = []
                borrow_tokens = []

                # 存入和奖励代币
                for token in detail.get("supply_token_list", []) + detail.get("reward_token_list", []):
                    add_token(assets, token.get("symbol", "?"), token.get("amount", 0), token.get("price", 0))
                    amount = token.get("amount", 0)
                    price = token.get("price", 0)
                    value = amount * price
                    if abs(value) >= 1:
                        supply_tokens.append({
                            "symbol": token.get("symbol", "?"),
                            "amount": Decimal(str(amount)),
                            "value": Decimal(str(value)),
                        })

                # 借入代币
                for token in detail.get("borrow_token_list", []):
                    add_token(debts, token.get("symbol", "?"), token.get("amount", 0), token.get("price", 0))
                    amount = token.get("amount", 0)
                    price = token.get("price", 0)
                    value = amount * price
                    if abs(value) >= 1:
                        borrow_tokens.append({
                            "symbol": token.get("symbol", "?"),
                            "amount": Decimal(str(amount)),
                            "value": Decimal(str(value)),
                        })

                if supply_tokens or borrow_tokens:
                    defi_positions.append({
                        "protocol": protocol_name,
                        "chain": protocol_chain,
                        "name": pos_name,
                        "supply": supply_tokens,
                        "borrow": borrow_tokens,
                        "detail": {
                            "health_rate": Decimal(str(detail.get("health_rate"))) if detail.get("health_rate") is not None else None,
                        },
                    })

        # Calculate quantities from value/price
        def finalize(target: dict) -> dict:
            result = {}
            for symbol, data in target.items():
                value = data["value"]
                price = data["price"]
                # For USD, quantity = value; for others, quantity = value / price
                if symbol == "USD":
                    quantity = value
                elif price > 0:
                    quantity = value / price
                else:
                    quantity = Decimal("0")
                result[symbol] = {
                    "quantity": quantity,
                    "value": value,
                    "price": price if symbol != "USD" else Decimal("1"),
                }
            return result

        assets = finalize(assets)
        debts = finalize(debts)

        total_assets = sum(d["value"] for d in assets.values())
        total_debts = sum(d["value"] for d in debts.values())

        return {
            "assets": assets,
            "debts": debts,
            "total_assets": total_assets,
            "total_debts": total_debts,
            "net_worth": total_assets - total_debts,
            "defi_positions": defi_positions,
        }

    # Chains to scan for NFTs (covers major EVM networks)
    NFT_CHAINS = ["eth", "arb", "bsc", "matic", "op", "base", "avax", "ron", "linea"]

    async def get_nft_portfolio(self, address: str, chains: list[str] | None = None) -> dict:
        """Get NFT portfolio with estimated values.

        Uses per-chain user/nft_list endpoint (all_nft_list requires higher API tier).

        Args:
            address: EVM address to scan.
            chains: Specific chains to scan. Defaults to NFT_CHAINS (all).

        Returns:
            {
                "nfts": [{"name": str, "collection": str, "chain": str,
                          "floor_price_usd": Decimal, "estimated_value_usd": Decimal}],
                "total_nft_value": Decimal,
            }
        """
        nfts = []
        total_value = Decimal("0")

        for chain_id in (chains or self.NFT_CHAINS):
            try:
                resp = await self._client.get(
                    f"{self.BASE_URL}/user/nft_list",
                    params={"id": address.lower(), "chain_id": chain_id},
                )
                resp.raise_for_status()
                items = resp.json()
            except Exception:
                logging.debug("Failed to fetch NFT list for %s on %s", address[:10], chain_id)
                continue

            for item in items:
                # user/nft_list: usd_price at top level, collection_name as string
                usd_price = float(item.get("usd_price") or 0)

                if usd_price < 500:
                    continue

                # pay_token.amount * pay_token.price = floor price (more reliable)
                pay_token = item.get("pay_token") or {}
                floor_amount = float(pay_token.get("amount") or 0)
                floor_token_price = float(pay_token.get("price") or 0)
                floor_price = floor_amount * floor_token_price

                # Prefer floor price when available; fall back to usd_price
                estimated_value = Decimal(str(floor_price)) if floor_price > 0 else Decimal(str(usd_price))

                nfts.append({
                    "name": item.get("name", "Unknown"),
                    "collection": item.get("collection_name") or item.get("contract_name") or "Unknown",
                    "chain": item.get("chain", chain_id),
                    "floor_price_usd": Decimal(str(floor_price)) if floor_price > 0 else Decimal("0"),
                    "estimated_value_usd": estimated_value,
                })
                total_value += estimated_value

        return {"nfts": nfts, "total_nft_value": total_value}

    async def get_positions(self, address: str) -> list[Position]:
        """Get all positions for aggregator compatibility."""
        portfolio = await self.get_portfolio(address)
        positions = []

        # Assets as a single wallet position
        if portfolio["assets"]:
            assets = [
                Asset(
                    asset_id=f"debank:{symbol}",
                    symbol=symbol,
                    quantity=info["quantity"],
                    price_usd=info["price"],
                    value_usd=info["value"],
                    source="debank",
                )
                for symbol, info in portfolio["assets"].items()
            ]
            positions.append(
                Position(
                    id=f"debank:portfolio:{address[:10]}",
                    type=PositionType.WALLET,
                    protocol="debank",
                    assets=assets,
                )
            )

        # Debts as liabilities
        if portfolio["debts"]:
            liabilities = [
                Liability(
                    asset_id=f"debank:{symbol}",
                    symbol=symbol,
                    quantity=info["quantity"],
                    price_usd=info["price"],
                    value_usd=info["value"],
                    source="debank",
                    protocol="debank",
                )
                for symbol, info in portfolio["debts"].items()
            ]
            positions.append(
                Position(
                    id=f"debank:debts:{address[:10]}",
                    type=PositionType.LENDING,
                    protocol="debank",
                    liabilities=liabilities,
                )
            )

        return positions

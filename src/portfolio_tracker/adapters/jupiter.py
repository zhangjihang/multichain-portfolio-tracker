"""Jupiter API adapter for Solana portfolio tracking.

Jupiter provides comprehensive Solana DeFi data including swaps,
perpetuals, and portfolio tracking across protocols.

API Docs: https://dev.jup.ag/api-reference
Portfolio: https://jup.ag/portfolio
"""

import logging
from decimal import Decimal

import httpx

from ..models import Asset, Liability, Position, PositionType


class JupiterAdapter:
    """Jupiter API adapter for Solana portfolio data.

    Jupiter aggregates:
    - Token prices and balances
    - DEX liquidity across Solana
    - Perpetual positions
    - Lending positions across protocols

    Note: Jupiter Portfolio API is in BETA as of 2026.
    """

    # Jupiter API endpoints
    PORTFOLIO_API = "https://api.jup.ag/portfolio/v1/positions"
    QUOTE_API = "https://api.jup.ag/swap/v1"

    # Raydium API for prices (free, no auth)
    RAYDIUM_PRICE_API = "https://api-v3.raydium.io/mint/price"
    RAYDIUM_INFO_API = "https://api-v3.raydium.io/mint/ids"

    def __init__(self, api_key: str | None = None):
        """Initialize Jupiter adapter.

        Args:
            api_key: Jupiter API key from portal.jup.ag (required for Portfolio API)
        """
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_token_prices(self, mint_addresses: list[str]) -> dict[str, Decimal]:
        """Get token prices from Raydium API.

        Args:
            mint_addresses: List of Solana token mint addresses

        Returns:
            Dict mapping mint address to USD price
        """
        if not mint_addresses:
            return {}

        prices = {}

        try:
            # Use Raydium API for Solana token prices (free, no auth required)
            resp = await self._client.get(
                "https://api-v3.raydium.io/mint/price",
                params={"mints": ",".join(mint_addresses)},
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("success") and "data" in data:
                for mint, price in data["data"].items():
                    if price is not None:
                        prices[mint] = Decimal(str(price))

            return prices
        except Exception:
            logging.exception("Failed to fetch Raydium prices for %s tokens", len(mint_addresses))
            return {}

    async def get_token_info(self, mint_addresses: list[str]) -> dict[str, dict]:
        """Get token metadata from Raydium API.

        Args:
            mint_addresses: List of Solana token mint addresses

        Returns:
            Dict mapping mint address to token info (symbol, name, decimals)
        """
        if not mint_addresses:
            return {}

        info = {}

        try:
            resp = await self._client.get(
                "https://api-v3.raydium.io/mint/ids",
                params={"mints": ",".join(mint_addresses)},
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("success") and "data" in data:
                for i, token_data in enumerate(data["data"]):
                    if token_data and i < len(mint_addresses):
                        mint = mint_addresses[i]
                        info[mint] = {
                            "symbol": token_data.get("symbol", mint[:8]),
                            "name": token_data.get("name", ""),
                            "decimals": token_data.get("decimals", 0),
                        }

            return info
        except Exception:
            logging.exception("Failed to fetch Raydium token info for %s tokens", len(mint_addresses))
            return {}

    async def get_defi_positions(self, address: str) -> dict:
        """Get DeFi positions from Jupiter Portfolio API.

        Returns staking, lending, LP positions etc.
        Requires Jupiter API key.

        Returns:
            {
                "positions": [{"label": str, "platform": str, "value": Decimal, "assets": [...]}],
                "total_value": Decimal,
            }
        """
        if not self.api_key:
            return {"positions": [], "total_value": Decimal("0")}

        try:
            headers = {"x-api-key": self.api_key}
            resp = await self._client.get(
                f"{self.PORTFOLIO_API}/{address}",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

            positions = []
            total_value = Decimal("0")
            token_info = data.get("tokenInfo", {}).get("solana", {})

            for element in data.get("elements", []):
                value = Decimal(str(element.get("value", 0)))
                if value < 1:
                    continue

                total_value += value

                position_assets = []
                element_data = element.get("data", {})

                for asset_data in element_data.get("assets", []):
                    if asset_data.get("type") == "token":
                        token = asset_data.get("data", {})
                        mint = token.get("address", "")
                        amount = Decimal(str(token.get("amount", 0)))
                        price = Decimal(str(token.get("price", 0)))
                        asset_value = Decimal(str(asset_data.get("value", 0)))

                        # Get symbol from token info
                        symbol = token_info.get(mint, {}).get("symbol", mint[:8])

                        position_assets.append({
                            "symbol": symbol,
                            "quantity": amount,
                            "price": price,
                            "value": asset_value,
                        })

                positions.append({
                    "label": element.get("label", "Unknown"),
                    "platform": element.get("platformId", ""),
                    "value": value,
                    "assets": position_assets,
                    "link": element_data.get("link", ""),
                })

            return {
                "positions": positions,
                "total_value": total_value,
            }
        except Exception:
            logging.exception("Failed to fetch Jupiter DeFi positions for %s", address)
            return {"positions": [], "total_value": Decimal("0")}

    async def get_token_balances_rpc(self, address: str) -> list[Asset]:
        """Get token balances using public Solana RPC.

        Fallback method when Helius API key is not available.
        """
        try:
            # Get SOL balance
            resp = await self._client.post(
                "https://api.mainnet-beta.solana.com",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [address],
                },
            )
            resp.raise_for_status()
            data = resp.json()

            assets = []
            if "result" in data and "value" in data["result"]:
                lamports = data["result"]["value"]
                sol_amount = Decimal(lamports) / Decimal(10**9)
                if sol_amount > 0:
                    # Get SOL price
                    prices = await self.get_token_prices(
                        ["So11111111111111111111111111111111111111112"]
                    )
                    sol_price = prices.get(
                        "So11111111111111111111111111111111111111112", Decimal("0")
                    )
                    assets.append(
                        Asset(
                            asset_id="solana:native",
                            symbol="SOL",
                            quantity=sol_amount,
                            price_usd=sol_price,
                            value_usd=sol_amount * sol_price,
                            source="jupiter:solana",
                        )
                    )

            # Get SPL token accounts
            resp = await self._client.post(
                "https://api.mainnet-beta.solana.com",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        address,
                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                        {"encoding": "jsonParsed"},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()

            if "result" not in data or "value" not in data["result"]:
                return assets

            mint_addresses = []
            token_data = []

            for account in data["result"]["value"]:
                try:
                    info = account["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    amount = info["tokenAmount"]
                    ui_amount = Decimal(str(amount.get("uiAmountString", "0")))

                    if ui_amount > 0:
                        token_data.append({
                            "mint": mint,
                            "amount": ui_amount,
                        })
                        mint_addresses.append(mint)
                except (KeyError, TypeError):
                    continue

            # Fetch prices and token info for all tokens
            if mint_addresses:
                prices = await self.get_token_prices(mint_addresses)
                token_info = await self.get_token_info(mint_addresses)

                for token in token_data:
                    mint = token["mint"]
                    price = prices.get(mint, Decimal("0"))
                    info = token_info.get(mint, {})
                    symbol = info.get("symbol", mint[:8])

                    assets.append(
                        Asset(
                            asset_id=f"solana:{mint}",
                            symbol=symbol,
                            quantity=token["amount"],
                            price_usd=price,
                            value_usd=token["amount"] * price,
                            source="jupiter:solana",
                        )
                    )

            return assets
        except Exception:
            logging.exception("Failed to fetch Solana wallet balances via Jupiter for %s", address)
            return []

    async def get_wallet_position(self, address: str) -> Position | None:
        """Get wallet token holdings as a position."""
        assets = await self.get_token_balances_rpc(address)

        if not assets:
            return None

        position = Position(
            id=f"jupiter:wallet:{address[:10]}",
            type=PositionType.WALLET,
            protocol="solana",
            chain="solana",
            assets=assets,
        )
        position.calculate_net_value()
        return position

    async def get_all_positions(self, address: str) -> list[Position]:
        """Get all Solana positions for an address.

        Currently returns wallet positions. Protocol positions
        (lending, LPs, perps) require additional integrations.
        """
        positions = []

        wallet = await self.get_wallet_position(address)
        if wallet:
            positions.append(wallet)

        return positions

    async def get_portfolio(self, address: str) -> dict:
        """Get aggregated portfolio matching DeBank format.

        Combines:
        1. Token balances from Solana RPC + Raydium prices
        2. DeFi positions from Jupiter Portfolio API (if api_key provided)

        Returns:
            {
                "assets": {"SOL": {"quantity": Decimal, "value": Decimal, "price": Decimal}, ...},
                "total_assets": Decimal,
            }
        """
        from .debank import TOKEN_CONVERT

        result_assets: dict[str, dict] = {}

        def add_asset(symbol: str, quantity: Decimal, price: Decimal, value: Decimal):
            base = TOKEN_CONVERT.get(symbol, symbol)
            if base not in result_assets:
                result_assets[base] = {"quantity": Decimal("0"), "value": Decimal("0"), "price": price}
            result_assets[base]["quantity"] += quantity
            result_assets[base]["value"] += value
            if symbol == base and price > 0:
                result_assets[base]["price"] = price

        # 1. Get token balances from RPC
        assets_list = await self.get_token_balances_rpc(address)
        for asset in assets_list:
            if asset.value_usd >= 1:
                add_asset(asset.symbol, asset.quantity, asset.price_usd, asset.value_usd)

        # 2. Get DeFi positions from Jupiter Portfolio API
        if self.api_key:
            defi_data = await self.get_defi_positions(address)
            for position in defi_data.get("positions", []):
                for asset in position.get("assets", []):
                    symbol = asset.get("symbol", "")
                    quantity = asset.get("quantity", Decimal("0"))
                    price = asset.get("price", Decimal("0"))
                    value = asset.get("value", Decimal("0"))
                    if value >= 1:
                        add_asset(symbol, quantity, price, value)

        total_assets = sum(a["value"] for a in result_assets.values())

        return {
            "assets": result_assets,
            "total_assets": total_assets,
        }

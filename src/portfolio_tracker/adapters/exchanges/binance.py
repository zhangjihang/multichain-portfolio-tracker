"""Binance exchange adapter."""

import hashlib
import hmac
import logging
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from ...models import Asset, Position, PositionType
from ..base import ExchangeAdapter


class BinanceAdapter(ExchangeAdapter):
    """Binance exchange adapter for spot, futures, margin, and earn."""

    BASE_URL = "https://api.binance.com"
    FUTURES_URL = "https://fapi.binance.com"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _sign(self, params: dict) -> str:
        """Generate HMAC SHA256 signature."""
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return signature

    async def _signed_request(
        self, method: str, url: str, params: dict | None = None
    ) -> dict:
        """Make a signed API request."""
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)

        headers = {"X-MBX-APIKEY": self.api_key}

        if method == "GET":
            resp = await self._client.get(url, params=params, headers=headers)
        else:
            resp = await self._client.post(url, params=params, headers=headers)

        resp.raise_for_status()
        return resp.json()

    async def get_spot_balances(self) -> list[Asset]:
        """Get spot wallet balances."""
        try:
            data = await self._signed_request(
                "GET", f"{self.BASE_URL}/api/v3/account"
            )
            assets = []
            for balance in data.get("balances", []):
                free = Decimal(balance["free"])
                locked = Decimal(balance["locked"])
                total = free + locked

                if total > 0:
                    symbol = balance["asset"]
                    assets.append(
                        Asset(
                            asset_id=f"exchange:binance:{symbol}",
                            symbol=symbol,
                            quantity=total,
                            source="binance",
                        )
                    )

            return assets
        except Exception:
            logging.exception("Failed to fetch Binance spot balances")
            return []

    async def get_futures_positions(self) -> list[Position]:
        """Get futures/perpetual positions and balances."""
        positions = []

        # U本位合约 (USDT-M Futures)
        try:
            data = await self._signed_request(
                "GET", f"{self.FUTURES_URL}/fapi/v2/account"
            )

            # 1. 获取合约账户余额（保证金）
            assets = []
            for asset_data in data.get("assets", []):
                balance = Decimal(asset_data.get("walletBalance", "0"))
                if balance > 0:
                    symbol = asset_data["asset"]
                    assets.append(
                        Asset(
                            asset_id=f"exchange:binance:futures:{symbol}",
                            symbol=symbol,
                            quantity=balance,
                            source="binance:futures",
                        )
                    )

            if assets:
                positions.append(
                    Position(
                        id="binance:futures:balance",
                        type=PositionType.PERP,
                        protocol="binance",
                        assets=assets,
                    )
                )

        except Exception:
            logging.exception("Failed to fetch Binance USDT-M futures account data")

        # 币本位合约 (COIN-M Futures)
        try:
            data = await self._signed_request(
                "GET", "https://dapi.binance.com/dapi/v1/account"
            )

            assets = []
            for asset_data in data.get("assets", []):
                balance = Decimal(asset_data.get("walletBalance", "0"))
                if balance > 0:
                    symbol = asset_data["asset"]
                    assets.append(
                        Asset(
                            asset_id=f"exchange:binance:futures-coin:{symbol}",
                            symbol=symbol,
                            quantity=balance,
                            source="binance:futures-coin",
                        )
                    )

            if assets:
                positions.append(
                    Position(
                        id="binance:futures-coin:balance",
                        type=PositionType.PERP,
                        protocol="binance",
                        assets=assets,
                    )
                )

        except Exception:
            logging.exception("Failed to fetch Binance COIN-M futures account data")

        return positions

    async def get_margin_positions(self) -> list[Position]:
        """Get cross margin + flexible loan positions."""
        positions = []
        from ...models import Liability

        # Flexible Loan first (need to know collateral coins to dedup from cross margin)
        loan_collateral_coins: set[str] = set()
        try:
            data = await self._signed_request(
                "GET", f"{self.BASE_URL}/sapi/v2/loan/flexible/ongoing/orders"
            )
            for order in data.get("rows", []):
                loan_coin = order.get("loanCoin", "")
                collateral_coin = order.get("collateralCoin", "")
                total_debt = Decimal(order.get("totalDebt", "0"))
                collateral_amount = Decimal(order.get("collateralAmount", "0"))

                loan_assets = []
                loan_liabilities = []

                if collateral_amount > 0:
                    loan_collateral_coins.add(collateral_coin)
                    loan_assets.append(Asset(
                        asset_id=f"exchange:binance:loan-collateral:{collateral_coin}",
                        symbol=collateral_coin,
                        quantity=collateral_amount,
                        source="binance:loan",
                    ))
                if total_debt > 0:
                    loan_liabilities.append(Liability(
                        asset_id=f"exchange:binance:loan-debt:{loan_coin}",
                        symbol=loan_coin,
                        quantity=total_debt,
                        source="binance:loan",
                        protocol="binance",
                    ))

                if loan_assets or loan_liabilities:
                    positions.append(Position(
                        id=f"binance:loan:{collateral_coin}-{loan_coin}",
                        type=PositionType.LENDING,
                        protocol="binance",
                        assets=loan_assets,
                        liabilities=loan_liabilities,
                    ))
        except Exception:
            logging.exception("Failed to fetch Binance flexible loan data")

        # Cross margin (exclude loan collateral coins to avoid double counting)
        try:
            data = await self._signed_request(
                "GET", f"{self.BASE_URL}/sapi/v1/margin/account"
            )
            assets = []
            liabilities = []

            for balance in data.get("userAssets", []):
                symbol = balance["asset"]
                if symbol in loan_collateral_coins:
                    continue
                free = Decimal(balance.get("free", "0"))
                locked = Decimal(balance.get("locked", "0"))
                borrowed = Decimal(balance.get("borrowed", "0"))
                interest = Decimal(balance.get("interest", "0"))
                total_owned = free + locked
                total_borrowed = borrowed + interest

                if total_owned > 0:
                    assets.append(Asset(
                        asset_id=f"exchange:binance:margin:{symbol}",
                        symbol=symbol,
                        quantity=total_owned,
                        source="binance:margin",
                    ))
                if total_borrowed > 0:
                    liabilities.append(Liability(
                        asset_id=f"exchange:binance:margin:{symbol}",
                        symbol=symbol,
                        quantity=total_borrowed,
                        source="binance:margin",
                        protocol="binance",
                    ))

            if assets or liabilities:
                positions.append(Position(
                    id="binance:margin:cross",
                    type=PositionType.MARGIN,
                    protocol="binance",
                    assets=assets,
                    liabilities=liabilities,
                ))
        except Exception:
            logging.exception("Failed to fetch Binance cross margin data")

        return positions

    async def get_earn_positions(self) -> list[Position]:
        """Get flexible/locked earn positions."""
        positions = []

        # Flexible savings
        try:
            data = await self._signed_request(
                "GET", f"{self.BASE_URL}/sapi/v1/simple-earn/flexible/position"
            )
            assets = []
            for row in data.get("rows", []):
                amount = Decimal(row.get("totalAmount", "0"))
                if amount > 0:
                    symbol = row.get("asset", "UNKNOWN")
                    assets.append(
                        Asset(
                            asset_id=f"exchange:binance:{symbol}",
                            symbol=symbol,
                            quantity=amount,
                            source="binance:earn:flexible",
                        )
                    )

            if assets:
                positions.append(
                    Position(
                        id="binance:earn:flexible",
                        type=PositionType.EARN,
                        protocol="binance",
                        assets=assets,
                    )
                )
        except Exception:
            logging.exception("Failed to fetch Binance flexible earn positions")

        # Locked savings
        try:
            data = await self._signed_request(
                "GET", f"{self.BASE_URL}/sapi/v1/simple-earn/locked/position"
            )
            assets = []
            for row in data.get("rows", []):
                amount = Decimal(row.get("amount", "0"))
                if amount > 0:
                    symbol = row.get("asset", "UNKNOWN")
                    assets.append(
                        Asset(
                            asset_id=f"exchange:binance:{symbol}",
                            symbol=symbol,
                            quantity=amount,
                            source="binance:earn:locked",
                        )
                    )

            if assets:
                positions.append(
                    Position(
                        id="binance:earn:locked",
                        type=PositionType.EARN,
                        protocol="binance",
                        assets=assets,
                    )
                )
        except Exception:
            logging.exception("Failed to fetch Binance locked earn positions")

        return positions

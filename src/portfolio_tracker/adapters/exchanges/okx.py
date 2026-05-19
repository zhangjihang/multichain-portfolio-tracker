"""OKX exchange adapter."""

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from ...models import Asset, Position, PositionType
from ..base import ExchangeAdapter


class OKXAdapter(ExchangeAdapter):
    """OKX exchange adapter for spot, funding, derivatives, and earn."""

    BASE_URL = "https://www.okx.com"

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        payload = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode()

    async def _request(
        self, method: str, path: str, params: dict | None = None
    ) -> dict:
        timestamp = self._timestamp()
        params = params or {}

        if method.upper() == "GET":
            query = urlencode(params) if params else ""
            request_path = f"{path}?{query}" if query else path
            body = ""
            url = f"{self.BASE_URL}{request_path}"
            signature = self._sign(timestamp, method, request_path, body)
            headers = {
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
            }
            resp = await self._client.get(url, headers=headers)
        else:
            body = json.dumps(params) if params else ""
            request_path = path
            url = f"{self.BASE_URL}{request_path}"
            signature = self._sign(timestamp, method, request_path, body)
            headers = {
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
            }
            resp = await self._client.post(url, content=body, headers=headers)

        resp.raise_for_status()
        return resp.json()

    async def get_spot_balances(self) -> list[Asset]:
        """Combine trading (account) and funding balances."""
        assets: dict[str, Decimal] = {}

        # Trading account
        try:
            data = await self._request("GET", "/api/v5/account/balance")
            for account in data.get("data", []):
                for detail in account.get("details", []):
                    symbol = detail.get("ccy")
                    if not symbol:
                        continue
                    qty = Decimal(detail.get("eq", detail.get("cashBal", "0") or "0"))
                    if qty > 0:
                        assets[symbol] = assets.get(symbol, Decimal("0")) + qty
        except Exception:
            logging.exception("Failed to fetch OKX account balances")

        # Funding account
        try:
            data = await self._request("GET", "/api/v5/asset/balances")
            for item in data.get("data", []):
                symbol = item.get("ccy")
                if not symbol:
                    continue
                qty = Decimal(item.get("availBal", item.get("bal", "0") or "0"))
                if qty > 0:
                    assets[symbol] = assets.get(symbol, Decimal("0")) + qty
        except Exception:
            logging.exception("Failed to fetch OKX funding balances")

        return [
            Asset(
                asset_id=f"exchange:okx:{symbol}",
                symbol=symbol,
                quantity=qty,
                source="okx:spot",
            )
            for symbol, qty in assets.items()
            if qty > 0
        ]

    async def get_futures_positions(self) -> list[Position]:
        """Get derivatives positions (perpetuals/futures)."""
        try:
            data = await self._request("GET", "/api/v5/account/positions")
            items = data.get("data", []) or []
            assets: list[Asset] = []

            for item in items:
                symbol = item.get("ccy") or item.get("marginCcy")
                if not symbol:
                    continue
                margin = Decimal(item.get("margin", item.get("posMargin", "0") or "0"))
                if margin > 0:
                    assets.append(
                        Asset(
                            asset_id=f"exchange:okx:perp:{symbol}",
                            symbol=symbol,
                            quantity=margin,
                            source="okx:perp",
                        )
                    )

            if assets:
                return [
                    Position(
                        id="okx:perp",
                        type=PositionType.PERP,
                        protocol="okx",
                        assets=assets,
                    )
                ]
            return []
        except Exception:
            logging.exception("Failed to fetch OKX positions")
            return []

    async def get_margin_positions(self) -> list[Position]:
        """OKX margin not covered explicitly."""
        return []

    async def get_earn_positions(self) -> list[Position]:
        """Get savings/earn balances including simple earn, staking/DeFi, and ETH staking."""
        positions: list[Position] = []

        # 1. Simple Earn (savings)
        try:
            data = await self._request("GET", "/api/v5/finance/savings/balance")
            items = data.get("data", []) or []
            assets: list[Asset] = []
            for item in items:
                symbol = item.get("ccy")
                if not symbol:
                    continue
                amount = Decimal(item.get("amt", item.get("balance", "0") or "0"))
                if amount > 0:
                    assets.append(
                        Asset(
                            asset_id=f"exchange:okx:earn:{symbol}",
                            symbol=symbol,
                            quantity=amount,
                            source="okx:earn",
                        )
                    )
            if assets:
                positions.append(
                    Position(
                        id="okx:earn",
                        type=PositionType.EARN,
                        protocol="okx",
                        assets=assets,
                    )
                )
        except Exception:
            logging.exception("Failed to fetch OKX simple earn balances")

        # 2. Staking/DeFi (on-chain earn/staking)
        try:
            data = await self._request("GET", "/api/v5/finance/staking-defi/orders-active")
            items = data.get("data", []) or []
            assets = []
            for item in items:
                symbol = item.get("ccy")
                if not symbol:
                    continue
                # Try investData[0].amt first, then amt
                invest_data = item.get("investData", [])
                if invest_data and isinstance(invest_data, list):
                    amount = Decimal(invest_data[0].get("amt", "0") or "0")
                else:
                    amount = Decimal(item.get("amt", "0") or "0")
                if amount > 0:
                    assets.append(
                        Asset(
                            asset_id=f"exchange:okx:staking:{symbol}",
                            symbol=symbol,
                            quantity=amount,
                            source="okx:staking",
                        )
                    )
            if assets:
                positions.append(
                    Position(
                        id="okx:staking",
                        type=PositionType.EARN,
                        protocol="okx",
                        assets=assets,
                    )
                )
        except Exception:
            logging.exception("Failed to fetch OKX staking/DeFi positions")

        # 3. ETH Staking
        try:
            data = await self._request("GET", "/api/v5/finance/staking-defi/eth/balance")
            items = data.get("data", []) or []
            assets = []
            for item in items:
                # ETH staking returns ETH balances
                amount = Decimal(item.get("amt", item.get("balance", "0") or "0"))
                if amount > 0:
                    assets.append(
                        Asset(
                            asset_id="exchange:okx:eth-staking:ETH",
                            symbol="ETH",
                            quantity=amount,
                            source="okx:eth-staking",
                        )
                    )
            if assets:
                positions.append(
                    Position(
                        id="okx:eth-staking",
                        type=PositionType.EARN,
                        protocol="okx",
                        assets=assets,
                    )
                )
        except Exception:
            logging.exception("Failed to fetch OKX ETH staking balances")

        return positions

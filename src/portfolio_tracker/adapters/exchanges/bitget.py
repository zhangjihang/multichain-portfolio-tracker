"""Bitget exchange adapter."""

import base64
import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from ...models import Asset, Liability, Position, PositionType
from ..base import ExchangeAdapter


class BitgetAdapter(ExchangeAdapter):
    """Bitget exchange adapter covering spot, futures, margin, and earn."""

    BASE_URL = "https://api.bitget.com"

    def __init__(self, api_key: str, api_secret: str, passphrase: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        """Generate Bitget signature."""
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode()

    async def _request(
        self, method: str, path: str, params: dict | None = None
    ) -> dict:
        """Make a signed request to Bitget."""
        timestamp = str(int(time.time() * 1000))
        params = params or {}

        if method.upper() == "GET":
            query = urlencode(params) if params else ""
            request_path = f"{path}?{query}" if query else path
            body = ""
            url = f"{self.BASE_URL}{request_path}"
            signature = self._sign(timestamp, method, request_path, body)
            headers = {
                "ACCESS-KEY": self.api_key,
                "ACCESS-SIGN": signature,
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
            }
            resp = await self._client.get(url, headers=headers)
        else:
            body = json.dumps(params) if params else ""
            request_path = path
            url = f"{self.BASE_URL}{request_path}"
            signature = self._sign(timestamp, method, request_path, body)
            headers = {
                "ACCESS-KEY": self.api_key,
                "ACCESS-SIGN": signature,
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
            }
            resp = await self._client.post(url, content=body, headers=headers)

        resp.raise_for_status()
        return resp.json()

    async def get_spot_balances(self) -> list[Asset]:
        """Get spot balances."""
        try:
            data = await self._request("GET", "/api/v2/spot/account/assets")
            items = data.get("data", []) or data.get("result", []) or []

            assets: list[Asset] = []
            for item in items:
                symbol = item.get("coin") or item.get("symbol") or item.get("currency")
                if not symbol:
                    continue
                available = Decimal(item.get("available", item.get("availableBalance", "0") or "0"))
                frozen = Decimal(item.get("frozen", item.get("locked", "0") or "0"))
                total = available + frozen
                if total > 0:
                    assets.append(
                        Asset(
                            asset_id=f"exchange:bitget:{symbol}",
                            symbol=symbol,
                            quantity=total,
                            source="bitget:spot",
                        )
                    )
            return assets
        except Exception:
            logging.exception("Failed to fetch Bitget spot balances")
            return []

    async def get_futures_positions(self) -> list[Position]:
        """Get USDT-M and COIN-M futures balances."""
        positions: list[Position] = []

        for product_type, label in [
            ("USDT-FUTURES", "bitget:futures:usdt"),
            ("COIN-FUTURES", "bitget:futures:coin"),
        ]:
            try:
                data = await self._request(
                    "GET",
                    "/api/v2/mix/account/accounts",
                    params={"productType": product_type},
                )
                items = data.get("data", []) or []
                assets: list[Asset] = []
                for item in items:
                    symbol = item.get("marginCoin") or item.get("coin") or item.get("currency")
                    equity = Decimal(
                        item.get("equity")
                        or item.get("available", item.get("availableBalance", "0") or "0")
                        or "0"
                    )
                    if equity > 0 and symbol:
                        assets.append(
                            Asset(
                                asset_id=f"exchange:bitget:{product_type.lower()}:{symbol}",
                                symbol=symbol,
                                quantity=equity,
                                source=f"bitget:{product_type.lower()}",
                            )
                        )

                if assets:
                    positions.append(
                        Position(
                            id=label,
                            type=PositionType.PERP,
                            protocol="bitget",
                            assets=assets,
                        )
                    )
            except Exception:
                logging.exception("Failed to fetch Bitget futures account data for %s", product_type)

        return positions

    async def get_margin_positions(self) -> list[Position]:
        """Get cross margin assets and liabilities."""
        try:
            data = await self._request("GET", "/api/v2/margin/crossed/account/assets")
            items = data.get("data", []) or []

            assets: list[Asset] = []
            liabilities: list[Liability] = []

            for item in items:
                symbol = item.get("coin") or item.get("symbol") or item.get("currency")
                if not symbol:
                    continue
                available = Decimal(item.get("available", "0") or "0")
                frozen = Decimal(item.get("frozen", "0") or "0")
                borrowed = Decimal(item.get("borrowed", item.get("liability", "0") or "0"))
                interest = Decimal(item.get("interest", "0") or "0")
                owned = available + frozen
                debt = borrowed + interest

                if owned > 0:
                    assets.append(
                        Asset(
                            asset_id=f"exchange:bitget:{symbol}",
                            symbol=symbol,
                            quantity=owned,
                            source="bitget:margin",
                        )
                    )
                if debt > 0:
                    liabilities.append(
                        Liability(
                            asset_id=f"exchange:bitget:{symbol}",
                            symbol=symbol,
                            quantity=debt,
                            source="bitget:margin",
                            protocol="bitget",
                        )
                    )

            if assets or liabilities:
                position = Position(
                    id="bitget:margin:cross",
                    type=PositionType.MARGIN,
                    protocol="bitget",
                    assets=assets,
                    liabilities=liabilities,
                )
                position.calculate_net_value()
                return [position]

            return []
        except Exception:
            logging.exception("Failed to fetch Bitget margin account data")
            return []

    async def _fetch_earn_product(self, path: str, source: str) -> list[Asset]:
        """Fetch assets from a single earn product endpoint."""
        try:
            data = await self._request("GET", path)
            raw = data.get("data", []) or []
            # Bitget earn endpoints return {"data": {"resultList": [...], "endId": "..."}}
            if isinstance(raw, dict):
                items = raw.get("resultList", []) or []
            elif isinstance(raw, list):
                items = raw
            else:
                items = []
            assets: list[Asset] = []

            for item in items:
                symbol = item.get("coin") or item.get("symbol") or item.get("currency")
                if not symbol:
                    continue
                amount = Decimal(item.get("amount", item.get("balance", "0") or "0"))
                if amount > 0:
                    assets.append(
                        Asset(
                            asset_id=f"exchange:bitget:{source.split(':')[-1]}:{symbol}",
                            symbol=symbol,
                            quantity=amount,
                            source=source,
                        )
                    )
            return assets
        except Exception:
            logging.warning("Failed to fetch Bitget earn product %s", path, exc_info=True)
            return []

    async def get_earn_positions(self) -> list[Position]:
        """Get all earn positions: savings, shark fin, and staking."""
        all_assets: list[Asset] = []

        # Savings
        savings = await self._fetch_earn_product(
            "/api/v2/earn/savings/assets", "bitget:earn:savings"
        )
        all_assets.extend(savings)

        # Shark Fin
        sharkfin = await self._fetch_earn_product(
            "/api/v2/earn/sharkfin/assets", "bitget:earn:sharkfin"
        )
        all_assets.extend(sharkfin)

        # Staking (Launchpool / PoS staking) — endpoint removed from Bitget API
        # Keeping code in case Bitget re-introduces it under a new path.
        # staking = await self._fetch_earn_product(
        #     "/api/v2/earn/staking/assets", "bitget:earn:staking"
        # )
        # all_assets.extend(staking)

        if all_assets:
            return [
                Position(
                    id="bitget:earn",
                    type=PositionType.EARN,
                    protocol="bitget",
                    assets=all_assets,
                )
            ]
        return []

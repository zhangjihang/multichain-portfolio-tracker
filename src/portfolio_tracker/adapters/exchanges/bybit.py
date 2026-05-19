"""Bybit exchange adapter."""

import hashlib
import hmac
import logging
import time
from decimal import Decimal

import httpx

from ...models import Asset, Liability, Position, PositionType
from ..base import ExchangeAdapter


class BybitAdapter(ExchangeAdapter):
    """Bybit exchange adapter for unified account (spot, derivatives, earn)."""

    BASE_URL = "https://api.bybit.com"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _sign(self, timestamp: str, params: str) -> str:
        """Generate HMAC SHA256 signature for Bybit API."""
        param_str = f"{timestamp}{self.api_key}{params}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _signed_request(self, method: str, endpoint: str, params: dict | None = None) -> dict:
        """Make a signed API request to Bybit."""
        timestamp = str(int(time.time() * 1000))
        params = params or {}

        if method == "GET":
            # For GET, params go in query string
            from urllib.parse import urlencode
            query_string = urlencode(params) if params else ""
            recv_window = "5000"
            sign_payload = f"{timestamp}{self.api_key}{recv_window}{query_string}"
            signature = hmac.new(
                self.api_secret.encode("utf-8"),
                sign_payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": signature,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
            }

            url = f"{self.BASE_URL}{endpoint}"
            resp = await self._client.get(url, params=params, headers=headers)
        else:
            # For POST, params go in body as JSON
            import json
            body = json.dumps(params) if params else ""
            recv_window = "5000"
            sign_payload = f"{timestamp}{self.api_key}{recv_window}{body}"
            signature = hmac.new(
                self.api_secret.encode("utf-8"),
                sign_payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": signature,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "Content-Type": "application/json",
            }

            url = f"{self.BASE_URL}{endpoint}"
            resp = await self._client.post(url, content=body, headers=headers)

        resp.raise_for_status()
        return resp.json()

    async def get_spot_balances(self) -> list[Asset]:
        """Get all account balances (unified + fund)."""
        assets = []

        # Unified Trading Account (UTA)
        try:
            data = await self._signed_request(
                "GET", "/v5/account/wallet-balance",
                params={"accountType": "UNIFIED"}
            )

            if data.get("retCode") == 0:
                for account in data.get("result", {}).get("list", []):
                    for coin in account.get("coin", []):
                        balance = Decimal(coin.get("walletBalance", "0") or "0")
                        if balance > 0:
                            symbol = coin.get("coin", "UNKNOWN")
                            assets.append(
                                Asset(
                                    asset_id=f"exchange:bybit:unified:{symbol}",
                                    symbol=symbol,
                                    quantity=balance,
                                    source="bybit:unified",
                                )
                            )
        except Exception:
            logging.exception("Failed to fetch Bybit unified account balances")

        # Funding account - use the asset transfer API
        try:
            data = await self._signed_request(
                "GET", "/v5/asset/transfer/query-account-coins-balance",
                params={"accountType": "FUND"}
            )

            if data.get("retCode") == 0:
                for coin in data.get("result", {}).get("balance", []):
                    balance = Decimal(coin.get("walletBalance", "0") or "0")
                    if balance > 0:
                        symbol = coin.get("coin", "UNKNOWN")
                        assets.append(
                            Asset(
                                asset_id=f"exchange:bybit:fund:{symbol}",
                                symbol=symbol,
                                quantity=balance,
                                source="bybit:fund",
                            )
                        )
        except Exception:
            logging.exception("Failed to fetch Bybit funding account balances")

        return assets

    async def get_futures_positions(self) -> list[Position]:
        """Get derivatives positions."""
        # Already included in unified account balance
        return []

    async def get_margin_positions(self) -> list[Position]:
        """Get margin positions (borrowing info)."""
        positions = []

        try:
            data = await self._signed_request(
                "GET", "/v5/account/borrow-history",
                params={"limit": "50"}
            )

            # Bybit unified account handles margin differently
            # Borrowing is reflected in the account equity

        except Exception:
            logging.exception("Failed to fetch Bybit margin borrowing history")

        return positions

    async def get_earn_positions(self) -> list[Position]:
        """Get earn/savings positions."""
        positions = []

        # Bybit Earn products
        try:
            data = await self._signed_request(
                "GET", "/v5/earn/position",
                params={}
            )

            if data.get("retCode") == 0:
                assets = []
                for item in data.get("result", {}).get("list", []):
                    quantity = Decimal(item.get("quantity", "0"))
                    if quantity > 0:
                        symbol = item.get("coin", "UNKNOWN")
                        assets.append(
                            Asset(
                                asset_id=f"exchange:bybit:earn:{symbol}",
                                symbol=symbol,
                                quantity=quantity,
                                source="bybit:earn",
                            )
                        )

                if assets:
                    positions.append(
                        Position(
                            id="bybit:earn",
                            type=PositionType.EARN,
                            protocol="bybit",
                            assets=assets,
                        )
                    )
        except Exception:
            logging.exception("Failed to fetch Bybit earn positions")

        return positions

    async def get_crypto_loan_positions(self) -> list[Position]:
        """Get Crypto Loan positions (collateral + debt)."""
        positions = []
        try:
            data = await self._signed_request(
                "GET", "/v5/crypto-loan-common/position", {}
            )
            if data.get("retCode") != 0:
                return positions

            result = data.get("result", {})
            assets = []
            liabilities = []

            # Collateral = assets locked
            for c in result.get("collateralList", []):
                qty = Decimal(c.get("amount", "0"))
                usd = Decimal(c.get("amountUSD", "0"))
                symbol = c.get("currency", "UNKNOWN")
                if qty > 0:
                    assets.append(
                        Asset(
                            asset_id=f"exchange:bybit:loan-collateral:{symbol}",
                            symbol=symbol,
                            quantity=qty,
                            source="bybit:loan-collateral",
                        )
                    )

            # Borrowed = liabilities
            for b in result.get("borrowList", []):
                flex_debt = Decimal(b.get("flexibleTotalDebt", "0"))
                fixed_debt = Decimal(b.get("fixedTotalDebt", "0"))
                total_debt = flex_debt + fixed_debt
                symbol = b.get("loanCurrency", "UNKNOWN")
                if total_debt > 0:
                    liabilities.append(
                        Liability(
                            asset_id=f"exchange:bybit:loan-debt:{symbol}",
                            symbol=symbol,
                            quantity=total_debt,
                            price_usd=Decimal("1") if symbol in ("USDC", "USDT", "DAI") else Decimal("0"),
                            value_usd=Decimal(b.get("flexibleTotalDebtUSD", "0")) + Decimal(b.get("fixedTotalDebtUSD", "0")),
                            source="bybit:loan",
                            protocol="bybit",
                        )
                    )

            # Supply = assets lent out (earning interest)
            for s in result.get("supplyList", []):
                qty = Decimal(s.get("amount", "0"))
                symbol = s.get("currency", "UNKNOWN")
                if qty > 0:
                    assets.append(
                        Asset(
                            asset_id=f"exchange:bybit:loan-supply:{symbol}",
                            symbol=symbol,
                            quantity=qty,
                            source="bybit:loan-supply",
                        )
                    )

            if assets or liabilities:
                positions.append(
                    Position(
                        id="bybit:crypto-loan",
                        type=PositionType.LENDING,
                        protocol="bybit",
                        assets=assets,
                        liabilities=liabilities,
                    )
                )

        except Exception:
            logging.exception("Failed to fetch Bybit crypto loan positions")

        return positions

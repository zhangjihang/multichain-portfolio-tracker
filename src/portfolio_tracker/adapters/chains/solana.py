"""Solana chain adapter."""

import logging
from decimal import Decimal

import httpx

from ...models import Asset
from ..base import ChainAdapter

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
SOL_DECIMALS = 9


class SolanaAdapter(ChainAdapter):
    """Solana chain adapter."""

    def __init__(self):
        self.rpc_url = SOLANA_RPC
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _rpc_call(self, method: str, params: list) -> dict:
        """Make a JSON-RPC call to Solana."""
        resp = await self._client.post(
            self.rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_native_balance(self, address: str) -> Asset:
        """Get SOL balance for an address."""
        try:
            result = await self._rpc_call("getBalance", [address])
            if "result" in result and "value" in result["result"]:
                lamports = result["result"]["value"]
                balance = Decimal(lamports) / Decimal(10**SOL_DECIMALS)
            else:
                balance = Decimal("0")
        except Exception:
            logging.exception("Failed to fetch SOL balance for %s", address)
            balance = Decimal("0")

        return Asset(
            asset_id="solana:native",
            symbol="SOL",
            quantity=balance,
            source="solana",
        )

    async def get_token_balances(self, address: str) -> list[Asset]:
        """Get SPL token balances for an address."""
        try:
            result = await self._rpc_call(
                "getTokenAccountsByOwner",
                [
                    address,
                    {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                    {"encoding": "jsonParsed"},
                ],
            )

            if "result" not in result or "value" not in result["result"]:
                return []

            assets = []
            for account in result["result"]["value"]:
                try:
                    info = account["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    amount = info["tokenAmount"]
                    ui_amount = Decimal(str(amount.get("uiAmountString", "0")))

                    if ui_amount > 0:
                        assets.append(
                            Asset(
                                asset_id=f"solana:{mint}",
                                symbol=mint[:8],  # Use truncated mint as symbol
                                quantity=ui_amount,
                                source="solana",
                            )
                        )
                except (KeyError, TypeError):
                    continue

            return assets
        except Exception:
            logging.exception("Failed to fetch Solana token balances for %s", address)
            return []

"""EVM chain adapter using public RPCs."""

import logging
import os
from decimal import Decimal

import httpx

from ...models import Asset
from ..base import ChainAdapter

# Public RPC endpoints
CHAIN_RPCS = {
    "ethereum": "https://eth.llamarpc.com",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "optimism": "https://mainnet.optimism.io",
    "polygon": "https://polygon-rpc.com",
    "base": "https://mainnet.base.org",
    "bsc": "https://bsc-dataseed.binance.org",
    "avalanche": "https://api.avax.network/ext/bc/C/rpc",
}

CHAIN_NATIVE_TOKENS = {
    "ethereum": ("ETH", 18),
    "arbitrum": ("ETH", 18),
    "optimism": ("ETH", 18),
    "polygon": ("MATIC", 18),
    "base": ("ETH", 18),
    "bsc": ("BNB", 18),
    "avalanche": ("AVAX", 18),
}

CHAIN_IDS = {
    "ethereum": 1,
    "arbitrum": 42161,
    "optimism": 10,
    "polygon": 137,
    "base": 8453,
    "bsc": 56,
    "avalanche": 43114,
}

# Etherscan-like API endpoints for token balances
EXPLORER_APIS = {
    "ethereum": "https://api.etherscan.io/api",
    "arbitrum": "https://api.arbiscan.io/api",
    "optimism": "https://api-optimistic.etherscan.io/api",
    "polygon": "https://api.polygonscan.com/api",
    "base": "https://api.basescan.org/api",
}


class EVMAdapter(ChainAdapter):
    """EVM chain adapter supporting multiple networks."""

    def __init__(self, chain: str = "ethereum"):
        if chain not in CHAIN_RPCS:
            raise ValueError(f"Unsupported chain: {chain}")

        self.chain = chain
        self.chain_id = CHAIN_IDS[chain]
        self.rpc_url = CHAIN_RPCS[chain]
        self.explorer_api = EXPLORER_APIS.get(chain)
        self.native_symbol, self.native_decimals = CHAIN_NATIVE_TOKENS[chain]
        self._client = httpx.AsyncClient(timeout=30.0)

        # Try to get API key from environment
        self.api_key = os.environ.get(f"{chain.upper()}_EXPLORER_API_KEY", "")

    async def close(self) -> None:
        await self._client.aclose()

    async def _rpc_call(self, method: str, params: list) -> dict:
        """Make a JSON-RPC call."""
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
        """Get native token balance."""
        result = await self._rpc_call("eth_getBalance", [address, "latest"])

        if "result" in result:
            balance_wei = int(result["result"], 16)
            balance = Decimal(balance_wei) / Decimal(10**self.native_decimals)
        else:
            balance = Decimal("0")

        return Asset(
            asset_id=f"eip155:{self.chain_id}:native",
            symbol=self.native_symbol,
            quantity=balance,
            source=self.chain,
        )

    async def get_token_balances(self, address: str) -> list[Asset]:
        """Get ERC20 token balances using explorer API."""
        if not self.explorer_api:
            return []

        try:
            resp = await self._client.get(
                self.explorer_api,
                params={
                    "module": "account",
                    "action": "tokentx",
                    "address": address,
                    "startblock": 0,
                    "endblock": 99999999,
                    "sort": "desc",
                    "apikey": self.api_key or "YourApiKeyToken",
                },
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "1" or not data.get("result"):
                return []

            # Extract unique tokens from transactions
            token_contracts: dict[str, dict] = {}
            for tx in data["result"]:
                contract = tx.get("contractAddress", "").lower()
                if contract and contract not in token_contracts:
                    token_contracts[contract] = {
                        "symbol": tx.get("tokenSymbol", "UNKNOWN"),
                        "decimals": int(tx.get("tokenDecimal", 18)),
                    }

            # Get balances for each token
            assets = []
            for contract, info in list(token_contracts.items())[:20]:  # Limit to 20 tokens
                balance = await self._get_token_balance(address, contract, info["decimals"])
                if balance > 0:
                    assets.append(
                        Asset(
                            asset_id=f"eip155:{self.chain_id}:{contract}",
                            symbol=info["symbol"],
                            quantity=balance,
                            source=self.chain,
                        )
                    )

            return assets
        except Exception:
            logging.exception("Failed to fetch token balances for %s on %s", address, self.chain)
            return []

    async def _get_token_balance(
        self, address: str, contract: str, decimals: int
    ) -> Decimal:
        """Get balance of a specific ERC20 token."""
        # balanceOf(address) function selector
        data = f"0x70a08231000000000000000000000000{address[2:].lower()}"

        try:
            result = await self._rpc_call(
                "eth_call",
                [{"to": contract, "data": data}, "latest"],
            )
            if "result" in result and result["result"] != "0x":
                balance_raw = int(result["result"], 16)
                return Decimal(balance_raw) / Decimal(10**decimals)
        except Exception:
            logging.exception("Failed to fetch token balance for %s on contract %s", address, contract)

        return Decimal("0")

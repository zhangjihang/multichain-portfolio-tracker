"""Helius DAS API adapter for Solana NFT discovery.

Uses Digital Asset Standard (DAS) API to fetch NFT holdings.
API Docs: https://docs.helius.dev/solana-apis/digital-asset-standard-das-api
"""

import logging
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)


async def get_solana_nfts(
    api_key: str,
    addresses: list[str],
    min_value_usd: float = 500,
) -> dict:
    """Fetch Solana NFT holdings for multiple addresses.

    Args:
        api_key: Helius API key.
        addresses: list of Solana addresses.
        min_value_usd: minimum value to include.

    Returns:
        {
            "nfts": [{"name", "collection", "chain", "floor_price_usd", "estimated_value_usd"}],
            "total_nft_value": Decimal,
        }
    """
    nfts: list[dict] = []
    total_value = Decimal("0")

    # Get SOL price for floor price conversion
    sol_price = Decimal("0")
    async with httpx.AsyncClient(timeout=15.0) as price_client:
        try:
            resp = await price_client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
            )
            if resp.status_code == 200:
                sol_price = Decimal(str(resp.json().get("solana", {}).get("usd", 0)))
        except Exception:
            logger.warning("Failed to fetch SOL price for NFT valuation")

    if sol_price == 0:
        sol_price = Decimal("90")  # fallback

    async with httpx.AsyncClient(timeout=30.0) as client:
        for address in addresses:
            try:
                page = 1
                while True:
                    resp = await client.post(
                        f"https://mainnet.helius-rpc.com/?api-key={api_key}",
                        json={
                            "jsonrpc": "2.0",
                            "id": "nft-scan",
                            "method": "getAssetsByOwner",
                            "params": {
                                "ownerAddress": address,
                                "page": page,
                                "limit": 1000,
                                "displayOptions": {"showCollectionMetadata": True},
                            },
                        },
                    )
                    resp.raise_for_status()
                    result = resp.json().get("result", {})
                    items = result.get("items", [])

                    for item in items:
                        # Only NFTs (not fungible tokens)
                        iface = item.get("interface", "")
                        if iface not in ("V1_NFT", "V2_NFT", "ProgrammableNFT", "MplCoreAsset"):
                            continue

                        # Get collection name
                        grouping = item.get("grouping", [])
                        collection_name = None
                        for g in grouping:
                            if g.get("group_key") == "collection":
                                coll_meta = g.get("collection_metadata", {})
                                collection_name = coll_meta.get("name")
                                break
                        if not collection_name:
                            collection_name = item.get("content", {}).get("metadata", {}).get("name", "Unknown")

                        nft_name = item.get("content", {}).get("metadata", {}).get("name", "Unknown")

                        # Check floor price from token_info or pricing
                        price_info = item.get("token_info", {})
                        floor_sol = float(price_info.get("price_info", {}).get("price_per_token", 0) or 0)
                        floor_usd = float(Decimal(str(floor_sol)) * sol_price) if floor_sol > 0 else 0

                        if floor_usd < min_value_usd:
                            continue

                        item_value = Decimal(str(floor_usd))
                        nfts.append({
                            "name": nft_name,
                            "collection": collection_name,
                            "chain": "solana",
                            "floor_price_usd": item_value,
                            "estimated_value_usd": item_value,
                        })
                        total_value += item_value

                    # Check pagination
                    if len(items) < 1000:
                        break
                    page += 1

            except Exception:
                logger.warning("Helius NFT fetch failed for %s", address[:10], exc_info=True)

    logger.info("Helius NFT: found %d Solana NFTs worth $%s", len(nfts), f"{total_value:,.0f}")
    return {"nfts": nfts, "total_nft_value": total_value}

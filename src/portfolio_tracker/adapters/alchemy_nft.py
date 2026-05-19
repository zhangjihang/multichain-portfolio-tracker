"""Alchemy NFT API adapter - free alternative to DeBank for NFT discovery.

Supports EVM chains: Ethereum, Polygon, Arbitrum, Optimism, Base, BSC (via config).
API Docs: https://docs.alchemy.com/reference/getnftsforowner-v3
"""

import logging
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

# Chain -> Alchemy subdomain mapping
CHAIN_ENDPOINTS = {
    "eth": "eth-mainnet",
    "matic": "polygon-mainnet",
    "arb": "arb-mainnet",
    "op": "opt-mainnet",
    "base": "base-mainnet",
}


def _parse_owned_nfts(data: dict, chain_id: str) -> list[dict]:
    """Parse one getNFTsForOwner page into NFT dicts.

    No price filtering: Alchemy floor is not trusted anymore (nftpricefloor.com
    is the source of truth, applied later in service.py). Only spam is excluded.
    """
    nfts: list[dict] = []
    for nft in data.get("ownedNfts", []):
        if nft.get("isSpam"):
            continue
        balance = int(nft.get("balance", "1") or "1")
        collection_name = (
            nft.get("collection", {}).get("name")
            or nft.get("contract", {}).get("name")
            or nft.get("name")
            or "Unknown"
        )
        for _ in range(balance):
            nfts.append({
                "name": nft.get("name") or nft.get("tokenId", "Unknown"),
                "collection": collection_name,
                "chain": chain_id,
                "floor_price_usd": Decimal("0"),
                "estimated_value_usd": Decimal("0"),
            })
    return nfts


async def get_nfts_for_addresses(
    api_key: str,
    addresses: list[str],
    chains: list[str] | None = None,
) -> dict:
    """Fetch NFT holdings for multiple addresses across chains.

    Pricing is NOT done here. nftpricefloor.com (in service.py) is the single
    source of truth for EVM NFT value; this only enumerates non-spam holdings.

    Returns:
        {
            "nfts": [{"name", "collection", "chain",
                      "floor_price_usd", "estimated_value_usd"}],
            "total_nft_value": Decimal,
        }
    """
    scan_chains = chains or list(CHAIN_ENDPOINTS.keys())
    nfts: list[dict] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for chain_id in scan_chains:
            subdomain = CHAIN_ENDPOINTS.get(chain_id)
            if not subdomain:
                continue

            for address in addresses:
                try:
                    page_key = None
                    while True:
                        params = {
                            "owner": address.lower(),
                            "withMetadata": "true",
                            "excludeFilters[]": "SPAM",
                            "pageSize": "100",
                        }
                        if page_key:
                            params["pageKey"] = page_key

                        resp = await client.get(
                            f"https://{subdomain}.g.alchemy.com/nft/v3/{api_key}/getNFTsForOwner",
                            params=params,
                        )
                        if resp.status_code == 429:
                            logger.warning("Alchemy NFT rate limited on %s", chain_id)
                            break
                        resp.raise_for_status()
                        data = resp.json()

                        nfts.extend(_parse_owned_nfts(data, chain_id))

                        page_key = data.get("pageKey")
                        if not page_key:
                            break

                except Exception:
                    logger.debug(
                        "Alchemy NFT fetch failed for %s on %s",
                        address[:10], chain_id, exc_info=True,
                    )

    logger.info("Alchemy NFT: enumerated %d NFTs across %d addresses (pricing deferred)",
                len(nfts), len(addresses))
    return {"nfts": nfts, "total_nft_value": Decimal("0")}

"""Integration test for the NFT aggregation loop body.

Covers the glue that _resolve_nft_value feeds: holdings list, [NFT] asset
aggregation (incl. multiple of the same collection), and the evm/solana
source-breakdown split.
"""

from decimal import Decimal

from portfolio_tracker.service import _aggregate_nft_holdings


FLOORS = {
    "Bored Ape Yacht Club": Decimal("24800.55"),
    "CryptoPunks": Decimal("72148.16"),
    "Cheap Collection": Decimal("50"),  # below the $100 dust floor
}


def _run(raw):
    all_assets: dict = {}
    holdings: list = []
    source = {"evm": Decimal("0"), "solana": Decimal("0")}
    _aggregate_nft_holdings(raw, FLOORS, all_assets, holdings, source)
    return all_assets, holdings, source


def test_evm_indexed_and_unindexed_and_solana_and_dust():
    raw = [
        # EVM indexed -> kept, evm bucket
        {"name": "BAYC #1", "collection": "Bored Ape Yacht Club", "chain": "eth",
         "estimated_value_usd": Decimal("999999")},
        # second of same EVM collection -> aggregates into one [NFT] asset, qty 2
        {"name": "BAYC #2", "collection": "Bored Ape Yacht Club", "chain": "base",
         "estimated_value_usd": Decimal("0")},
        # EVM unindexed (homoglyph spoof) -> dropped entirely
        {"name": "POLlTlCS", "collection": "POLlTlCS IS BULLSHlT", "chain": "base",
         "estimated_value_usd": Decimal("81589.2")},
        # Solana, no floor -> falls back to Helius estimate, solana bucket
        {"name": "Mad Lad #7", "collection": "Mad Lads", "chain": "solana",
         "estimated_value_usd": Decimal("4500")},
        # EVM indexed but floor below $100 dust floor -> dropped
        {"name": "Dust", "collection": "Cheap Collection", "chain": "eth",
         "estimated_value_usd": Decimal("0")},
    ]
    all_assets, holdings, source = _run(raw)

    # Unindexed EVM and dust are dropped; 3 holdings remain
    names = sorted(h["name"] for h in holdings)
    assert names == ["BAYC #1", "BAYC #2", "Mad Lad #7"]
    assert "POLlTlCS" not in names

    # Two BAYC aggregate into a single [NFT] asset, qty 2, value summed
    bayc = all_assets["[NFT]Bored Ape Yacht Club"]
    assert bayc["quantity"] == Decimal("2")
    assert bayc["value"] == Decimal("24800.55") * 2
    assert bayc["price"] == Decimal("24800.55")

    # Mad Lads priced from Helius estimate (Solana fallback)
    assert all_assets["[NFT]Mad Lads"]["value"] == Decimal("4500")

    # Unindexed / dust never created an asset
    assert "[NFT]POLlTlCS IS BULLSHlT" not in all_assets
    assert "[NFT]Cheap Collection" not in all_assets

    # Source split: two BAYC -> evm; Mad Lads -> solana
    assert source["evm"] == Decimal("24800.55") * 2
    assert source["solana"] == Decimal("4500")


def test_empty_input_is_noop():
    all_assets, holdings, source = _run([])
    assert holdings == []
    assert all_assets == {}
    assert source == {"evm": Decimal("0"), "solana": Decimal("0")}

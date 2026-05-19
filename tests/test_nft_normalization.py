"""NFT collection-name normalization + slug resolution.

Alchemy returns collection names polluted with invisible joiner/zero-width
chars and single-letter spacing ("G r i f t e r s by X C O P Y"). That split
one BAYC collection into 3 rows and made legit collections 404 on
nftpricefloor. Normalizing the name before slug lookup and aggregation fixes
both, deterministically (no fuzzy matching).
"""

from decimal import Decimal

from portfolio_tracker.service import (
    _normalize_collection_name,
    _collection_to_slug,
    _aggregate_nft_holdings,
)

# Real invisible chars seen in Alchemy data (combining grapheme joiner U+034F)
BAYC_A = "Bored Ape Yacht Club ͏͏"
BAYC_B = "Bored Ape Yacht Club"
BAYC_C = "Bored͏ Ape Yacht Club"


def test_zero_width_variants_collapse_to_same_name():
    assert _normalize_collection_name(BAYC_A) == "Bored Ape Yacht Club"
    assert _normalize_collection_name(BAYC_B) == "Bored Ape Yacht Club"
    assert _normalize_collection_name(BAYC_C) == "Bored Ape Yacht Club"


def test_letter_spacing_collapsed():
    assert _normalize_collection_name("G r i f t e r s by X C O P Y") == "Grifters by XCOPY"
    assert _normalize_collection_name("POLITICS IS BULLSHIT by B E E P L E") == "POLITICS IS BULLSHIT by BEEPLE"
    assert _normalize_collection_name("X C O P Y - MAX PAIN AND FRENS") == "XCOPY - MAX PAIN AND FRENS"


def test_normal_names_unchanged():
    for n in ("CryptoPunks", "Azuki", "Mutant Ape Yacht Club", "Bad Bunnz"):
        assert _normalize_collection_name(n) == n


def test_slug_resolution_after_normalization():
    # Garbled letter-spaced name resolves to the real nftpricefloor slug
    assert _collection_to_slug("G r i f t e r s by X C O P Y") == "grifters-by-xcopy"
    # Zero-width BAYC variant resolves via the slug map
    assert _collection_to_slug(BAYC_A) == "bored-ape-yacht-club"
    # Curated entries for collections whose nftpricefloor slug is irregular
    assert _collection_to_slug("Memelands MVP") == "youtherealmvp"
    assert _collection_to_slug("X C O P Y - MAX PAIN AND FRENS") == "max-pain-and-frens-by-xcopy"


def test_aggregation_merges_zero_width_variants():
    """The 3 BAYC name variants must aggregate into ONE [NFT] asset."""
    floors = {"Bored Ape Yacht Club": Decimal("21345")}
    raw = [
        {"name": "#1", "collection": BAYC_A, "chain": "eth"},
        {"name": "#2", "collection": BAYC_B, "chain": "eth"},
        {"name": "#3", "collection": BAYC_C, "chain": "eth"},
    ]
    all_assets: dict = {}
    holdings: list = []
    source = {"evm": Decimal("0"), "solana": Decimal("0")}
    _aggregate_nft_holdings(raw, floors, all_assets, holdings, source)

    nft_keys = [k for k in all_assets if k.startswith("[NFT]")]
    assert nft_keys == ["[NFT]Bored Ape Yacht Club"], nft_keys
    assert all_assets["[NFT]Bored Ape Yacht Club"]["quantity"] == Decimal("3")
    assert all_assets["[NFT]Bored Ape Yacht Club"]["value"] == Decimal("21345") * 3
    assert len(holdings) == 3
    assert all(h["collection"] == "Bored Ape Yacht Club" for h in holdings)


def test_aggregate_handles_missing_nft_name():
    """A malformed NFT item (no 'name') must not crash the whole NFT pass."""
    floors = {"Bored Ape Yacht Club": Decimal("21345")}
    raw = [
        {"collection": "Bored Ape Yacht Club", "chain": "eth"},        # no "name"
        {"name": "ok", "collection": "Bored Ape Yacht Club", "chain": "eth"},
    ]
    all_assets: dict = {}
    holdings: list = []
    source = {"evm": Decimal("0"), "solana": Decimal("0")}
    _aggregate_nft_holdings(raw, floors, all_assets, holdings, source)
    assert len(holdings) == 2
    assert all_assets["[NFT]Bored Ape Yacht Club"]["quantity"] == Decimal("2")

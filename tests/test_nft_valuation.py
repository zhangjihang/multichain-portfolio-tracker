"""Tests for per-NFT value resolution (chain routing)."""

from decimal import Decimal

from portfolio_tracker.service import _resolve_nft_value


FLOORS = {"Bored Ape Yacht Club": Decimal("24800.55")}


def test_evm_with_floor_uses_nftpricefloor():
    nft = {"collection": "Bored Ape Yacht Club", "chain": "eth",
           "estimated_value_usd": Decimal("999999")}
    assert _resolve_nft_value(nft, FLOORS) == 24800.55


def test_evm_without_floor_is_dropped():
    nft = {"collection": "POLlTlCS IS BULLSHlT by B E E P L E", "chain": "base",
           "estimated_value_usd": Decimal("81589.2")}
    # 没收录 → None（剔除），不回退 Alchemy 估值
    assert _resolve_nft_value(nft, FLOORS) is None


def test_solana_with_floor_uses_nftpricefloor():
    nft = {"collection": "Bored Ape Yacht Club", "chain": "solana",
           "estimated_value_usd": Decimal("123")}
    assert _resolve_nft_value(nft, FLOORS) == 24800.55


def test_solana_without_floor_falls_back_to_estimate():
    nft = {"collection": "Mad Lads", "chain": "solana",
           "estimated_value_usd": Decimal("4500")}
    # Solana 路径不变：无 nftpricefloor 匹配时回退 Helius 估值
    assert _resolve_nft_value(nft, FLOORS) == 4500.0

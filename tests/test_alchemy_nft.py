"""Tests for Alchemy NFT response parsing."""

from decimal import Decimal

from portfolio_tracker.adapters.alchemy_nft import _parse_owned_nfts


def _resp():
    # 模拟一页 getNFTsForOwner 响应：含 spam、零 floor、正常三类
    return {
        "ownedNfts": [
            {
                "name": "Real #1",
                "collection": {"name": "Real Collection"},
                "contract": {"name": "RC", "openSeaMetadata": {"floorPrice": 0}},
                "balance": "1",
                "isSpam": False,
            },
            {
                "name": "Spammy",
                "collection": {"name": "Spam Collection"},
                "contract": {"name": "SC"},
                "balance": "1",
                "isSpam": True,
            },
            {
                "name": "Multi",
                "collection": {"name": "Multi Collection"},
                "contract": {"name": "MC", "openSeaMetadata": {"floorPrice": 0.001}},
                "balance": "2",
                "isSpam": False,
            },
        ]
    }


def test_parse_keeps_zero_floor_nfts_and_drops_spam():
    nfts = _parse_owned_nfts(_resp(), "base")

    names = [n["name"] for n in nfts]
    # 零 floor 的正版仍保留（不再按 Alchemy floor 预过滤）
    assert "Real #1" in names
    # spam 永远剔除
    assert "Spammy" not in names
    # balance=2 展开成两条
    assert names.count("Multi") == 2

    real = next(n for n in nfts if n["name"] == "Real #1")
    assert real["collection"] == "Real Collection"
    assert real["chain"] == "base"
    # 结构兼容：estimated_value_usd 存在且为 0
    assert real["estimated_value_usd"] == Decimal("0")

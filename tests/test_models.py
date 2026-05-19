"""Tests for data models."""

from decimal import Decimal

from portfolio_tracker.models import (
    Asset,
    Liability,
    PortfolioSnapshot,
    Position,
    PositionType,
)


def test_asset_calculate_value():
    """Test asset value calculation."""
    asset = Asset(
        asset_id="eip155:1:native",
        symbol="ETH",
        quantity=Decimal("2.5"),
        price_usd=Decimal("2000"),
        source="ethereum",
    )
    asset.calculate_value()
    assert asset.value_usd == Decimal("5000")


def test_liability_calculate_value():
    """Test liability value calculation."""
    liability = Liability(
        asset_id="eip155:1:0x...",
        symbol="USDC",
        quantity=Decimal("1000"),
        price_usd=Decimal("1"),
        source="aave",
        protocol="aave_v3",
    )
    liability.calculate_value()
    assert liability.value_usd == Decimal("1000")


def test_position_calculate_net_value():
    """Test position net value calculation."""
    position = Position(
        id="test:position",
        type=PositionType.LENDING,
        protocol="aave_v3",
        chain="ethereum",
        assets=[
            Asset(
                asset_id="eip155:1:native",
                symbol="ETH",
                quantity=Decimal("2"),
                price_usd=Decimal("2000"),
                value_usd=Decimal("4000"),
                source="aave",
            )
        ],
        liabilities=[
            Liability(
                asset_id="eip155:1:0x...",
                symbol="USDC",
                quantity=Decimal("1000"),
                price_usd=Decimal("1"),
                value_usd=Decimal("1000"),
                source="aave",
                protocol="aave_v3",
            )
        ],
    )
    position.calculate_net_value()
    assert position.net_value_usd == Decimal("3000")


def test_portfolio_snapshot_calculate_totals():
    """Test portfolio snapshot totals calculation."""
    snapshot = PortfolioSnapshot(
        positions=[
            Position(
                id="pos1",
                type=PositionType.WALLET,
                protocol="ethereum",
                assets=[
                    Asset(
                        asset_id="eip155:1:native",
                        symbol="ETH",
                        quantity=Decimal("1"),
                        value_usd=Decimal("2000"),
                        source="ethereum",
                    )
                ],
            ),
            Position(
                id="pos2",
                type=PositionType.LENDING,
                protocol="aave_v3",
                assets=[
                    Asset(
                        asset_id="eip155:1:0x...",
                        symbol="WETH",
                        quantity=Decimal("0.5"),
                        value_usd=Decimal("1000"),
                        source="aave",
                    )
                ],
                liabilities=[
                    Liability(
                        asset_id="eip155:1:0x...",
                        symbol="USDC",
                        quantity=Decimal("500"),
                        value_usd=Decimal("500"),
                        source="aave",
                        protocol="aave_v3",
                    )
                ],
            ),
        ]
    )
    snapshot.calculate_totals()

    assert snapshot.gross_assets_usd == Decimal("3000")
    assert snapshot.total_liabilities_usd == Decimal("500")
    assert snapshot.net_worth_usd == Decimal("2500")

"""Data models for portfolio tracking."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PositionType(str, Enum):
    WALLET = "wallet"
    LENDING = "lending"
    LP = "lp"
    PERP = "perp"
    SPOT = "spot"
    MARGIN = "margin"
    EARN = "earn"
    NFT = "nft"


class Asset(BaseModel):
    """Represents an owned asset."""

    asset_id: str = Field(description="Unique identifier: eip155:1:0x... / solana:... / exchange:binance:BTC")
    symbol: str
    quantity: Decimal
    price_usd: Decimal = Decimal("0")
    value_usd: Decimal = Decimal("0")
    source: str = Field(description="Source identifier (chain name, exchange name, etc.)")

    def calculate_value(self) -> None:
        """Calculate USD value from quantity and price."""
        self.value_usd = self.quantity * self.price_usd


class Liability(BaseModel):
    """Represents a debt/liability."""

    asset_id: str
    symbol: str
    quantity: Decimal
    price_usd: Decimal = Decimal("0")
    value_usd: Decimal = Decimal("0")
    source: str
    protocol: str = Field(description="Lending protocol name")

    def calculate_value(self) -> None:
        """Calculate USD value from quantity and price."""
        self.value_usd = self.quantity * self.price_usd


class Position(BaseModel):
    """Represents a position (wallet, lending, LP, etc.)."""

    id: str
    type: PositionType
    protocol: str = Field(description="Protocol/exchange name: Aave, Uniswap, binance")
    chain: Optional[str] = None
    assets: list[Asset] = Field(default_factory=list)
    liabilities: list[Liability] = Field(default_factory=list)
    net_value_usd: Decimal = Decimal("0")

    def calculate_net_value(self) -> None:
        """Calculate net value from assets and liabilities."""
        total_assets = sum(a.value_usd for a in self.assets)
        total_liabilities = sum(l.value_usd for l in self.liabilities)
        self.net_value_usd = total_assets - total_liabilities


class PortfolioSnapshot(BaseModel):
    """Complete portfolio snapshot at a point in time."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    positions: list[Position] = Field(default_factory=list)
    gross_assets_usd: Decimal = Decimal("0")
    total_liabilities_usd: Decimal = Decimal("0")
    net_worth_usd: Decimal = Decimal("0")

    def calculate_totals(self) -> None:
        """Calculate aggregate totals from all positions."""
        self.gross_assets_usd = sum(
            sum(a.value_usd for a in p.assets) for p in self.positions
        )
        self.total_liabilities_usd = sum(
            sum(l.value_usd for l in p.liabilities) for p in self.positions
        )
        self.net_worth_usd = self.gross_assets_usd - self.total_liabilities_usd

"""Abstract base classes for adapters."""

from abc import ABC, abstractmethod
from decimal import Decimal

from ..models import Asset, Position


class ChainAdapter(ABC):
    """Base class for blockchain adapters."""

    @abstractmethod
    async def get_native_balance(self, address: str) -> Asset:
        """Get native token balance for an address."""
        ...

    @abstractmethod
    async def get_token_balances(self, address: str) -> list[Asset]:
        """Get all token balances for an address."""
        ...


class ExchangeAdapter(ABC):
    """Base class for exchange adapters."""

    @abstractmethod
    async def get_spot_balances(self) -> list[Asset]:
        """Get spot wallet balances."""
        ...

    @abstractmethod
    async def get_futures_positions(self) -> list[Position]:
        """Get futures/perpetual positions."""
        ...

    @abstractmethod
    async def get_margin_positions(self) -> list[Position]:
        """Get margin positions."""
        ...

    @abstractmethod
    async def get_earn_positions(self) -> list[Position]:
        """Get earn/staking positions."""
        ...


class PriceProvider(ABC):
    """Base class for price data providers."""

    @abstractmethod
    async def get_price(self, asset_id: str) -> Decimal:
        """Get current USD price for an asset."""
        ...

    @abstractmethod
    async def get_prices(self, asset_ids: list[str]) -> dict[str, Decimal]:
        """Get current USD prices for multiple assets."""
        ...

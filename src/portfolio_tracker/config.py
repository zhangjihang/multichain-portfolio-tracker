"""Configuration loading and validation."""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from .utils.keychain import get_secret


def _load_dotenv() -> None:
    """Load .env file from project root into os.environ (once)."""
    if getattr(_load_dotenv, "_done", False):
        return
    _load_dotenv._done = True  # type: ignore[attr-defined]
    # Walk up from this file to find .env
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key_val = line.split("=", 1)
        if len(key_val) == 2:
            os.environ.setdefault(key_val[0].strip(), key_val[1].strip())


def _get_credential(key: str) -> Optional[str]:
    """Get a credential from Keychain first, then fall back to env var / .env file."""
    _load_dotenv()
    return get_secret(key) or os.environ.get(key)


class ExchangeConfig(BaseModel):
    """Configuration for an exchange."""

    name: str
    api_key_env: str
    api_secret_env: str
    passphrase_env: str | None = None

    def get_api_key(self) -> Optional[str]:
        return _get_credential(self.api_key_env)

    def get_api_secret(self) -> Optional[str]:
        return _get_credential(self.api_secret_env)

    def get_passphrase(self) -> Optional[str]:
        if self.passphrase_env:
            return _get_credential(self.passphrase_env)
        return None

    def create_adapter(self):
        """Create an exchange adapter instance. Returns None if credentials are missing."""
        from .adapters.exchanges.binance import BinanceAdapter
        from .adapters.exchanges.bybit import BybitAdapter
        from .adapters.exchanges.bitget import BitgetAdapter
        from .adapters.exchanges.okx import OKXAdapter

        key = self.get_api_key()
        secret = self.get_api_secret()
        if not key or not secret:
            return None
        pw = self.get_passphrase() or ""
        adapters = {
            "binance": lambda: BinanceAdapter(key, secret),
            "bybit": lambda: BybitAdapter(key, secret),
            "bitget": lambda: BitgetAdapter(key, secret, pw),
            "okx": lambda: OKXAdapter(key, secret, pw) if pw else None,
        }
        factory = adapters.get(self.name.lower())
        if factory:
            return factory()
        return None


class AggregatorConfig(BaseModel):
    """Configuration for data aggregators (DeBank, Jupiter)."""

    # DeBank for EVM chains - covers all DeFi protocols
    use_debank: bool = False
    debank_api_key_env: str = "DEBANK_API_KEY"

    # Jupiter for Solana DeFi positions
    use_jupiter: bool = False
    jupiter_api_key_env: str = "JUPITER_API_KEY"

    # Alchemy for EVM NFT discovery (free, replaces DeBank NFT scanning)
    alchemy_api_key_env: str = "ALCHEMY_API_KEY"

    # Helius for Solana NFT discovery
    helius_api_key_env: str = "HELIUS_API_KEY"

    def get_debank_api_key(self) -> Optional[str]:
        return _get_credential(self.debank_api_key_env)

    def get_jupiter_api_key(self) -> Optional[str]:
        return _get_credential(self.jupiter_api_key_env)

    def get_alchemy_api_key(self) -> Optional[str]:
        return _get_credential(self.alchemy_api_key_env)

    def get_helius_api_key(self) -> Optional[str]:
        return _get_credential(self.helius_api_key_env)


class PricingConfig(BaseModel):
    """Configuration for pricing providers."""

    provider_priority: list[str] = Field(default_factory=lambda: ["coingecko"])
    cache_ttl: int = 300


class ReportConfig(BaseModel):
    """Configuration for report generation."""

    output_dir: str = "./reports"
    top_assets_n: int = 10


class Config(BaseModel):
    """Main configuration model."""

    evm_addresses: list[str] = Field(default_factory=list)
    solana_addresses: list[str] = Field(default_factory=list)
    enabled_chains: list[str] = Field(default_factory=lambda: ["ethereum"])
    exchanges: list[ExchangeConfig] = Field(default_factory=list)
    aggregators: AggregatorConfig = Field(default_factory=AggregatorConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    discord_webhook_url: str | None = None
    alert_check_interval: int = 3600
    alert_cooldown: int = 7200
    db_path: str = "./data/portfolio.db"


def load_config(config_path: str | Path | None = None) -> Config:
    """Load configuration from a YAML file. Falls back to config.yaml or empty config."""
    if config_path is None:
        for candidate in ["config.yaml", "config.yml", "/app/config.yaml"]:
            if Path(candidate).exists():
                config_path = candidate
                break
    if config_path is None:
        return Config()
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    return Config.model_validate(data)

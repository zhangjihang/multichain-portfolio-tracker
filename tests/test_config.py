"""Tests for configuration loading."""

import tempfile
from pathlib import Path

import pytest

from portfolio_tracker.config import Config, load_config


def test_load_config_minimal():
    """Test loading a minimal config file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("evm_addresses:\n  - '0x1234'\n")
        f.flush()

        config = load_config(f.name)
        assert config.evm_addresses == ["0x1234"]
        assert config.enabled_chains == ["ethereum"]  # default
        assert config.pricing.cache_ttl == 300  # default


def test_load_config_full():
    """Test loading a full config file."""
    yaml_content = """
evm_addresses:
  - "0xabc"
  - "0xdef"
solana_addresses:
  - "soladdr1"
enabled_chains:
  - ethereum
  - arbitrum
exchanges:
  - name: binance
    api_key_env: BINANCE_KEY
    api_secret_env: BINANCE_SECRET
pricing:
  provider_priority:
    - coingecko
  cache_ttl: 600
report:
  output_dir: /tmp/reports
  top_assets_n: 5
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_config(f.name)
        assert len(config.evm_addresses) == 2
        assert len(config.solana_addresses) == 1
        assert "arbitrum" in config.enabled_chains
        assert len(config.exchanges) == 1
        assert config.exchanges[0].name == "binance"
        assert config.pricing.cache_ttl == 600
        assert config.report.top_assets_n == 5


def test_load_config_not_found():
    """Test loading a non-existent config file."""
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")


def test_config_defaults():
    """Test Config model defaults."""
    config = Config()
    assert config.evm_addresses == []
    assert config.solana_addresses == []
    assert config.enabled_chains == ["ethereum"]
    assert config.exchanges == []
    assert config.pricing.provider_priority == ["coingecko"]
    assert config.pricing.cache_ttl == 300
    assert config.report.output_dir == "./reports"
    assert config.report.top_assets_n == 10

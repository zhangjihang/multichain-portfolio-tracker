"""Tests for report_formatter — runs entirely offline using fixture data."""

import json
from pathlib import Path

import pytest

from portfolio_tracker.report_formatter import (
    _chain_name,
    _fmt_qty,
    _fmt_price,
    _fmt_val,
    _short_addr,
    format_report,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def sample_data():
    return _load("sample_portfolio.json")


@pytest.fixture
def prev_data():
    return _load("sample_portfolio_prev.json")


# --- Helper function tests ---

class TestHelpers:
    def test_chain_name_known(self):
        assert _chain_name("eth") == "Ethereum"
        assert _chain_name("arb") == "Arbitrum"
        assert _chain_name("sol") == "Solana"

    def test_chain_name_unknown(self):
        assert _chain_name("zora") == "Zora"
        assert _chain_name("") == "?"

    def test_fmt_qty_ranges(self):
        assert "M" in _fmt_qty(2_500_000)
        assert "K" in _fmt_qty(50_000)
        assert "." in _fmt_qty(5.5)
        assert "." in _fmt_qty(0.0012)

    def test_fmt_price_ranges(self):
        assert _fmt_price(67500) == "$67,500"
        assert _fmt_price(2.5) == "$2.50"
        assert _fmt_price(0.05) == "$0.0500"
        assert "$0.00" in _fmt_price(0.001)

    def test_fmt_val_ranges(self):
        assert "M" in _fmt_val(5_000_000)
        assert "K" in _fmt_val(25_000)
        assert "$" in _fmt_val(500)

    def test_short_addr(self):
        addr = "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12"
        assert _short_addr(addr) == "0xAbCd...Ef12"
        assert _short_addr("short") == "short"
        assert _short_addr("") == ""
        assert _short_addr(None) == ""


# --- format_report tests ---

class TestFormatReport:
    def test_current_report_basic_structure(self, sample_data):
        report = format_report(sample_data)

        # Summary line
        assert "净资产" in report
        assert "168,375" in report

        # Source breakdown (split into EVM / Solana / 交易所)
        assert "EVM" in report
        assert "Solana" in report
        assert "交易所" in report

        # Exchange section
        assert "Binance" in report or "binance" in report.lower()
        assert "Bybit" in report or "bybit" in report.lower()
        assert "健康度" in report

        # Assets table
        assert "主要持仓" in report
        assert "```" in report  # code block
        assert "BTC" in report
        assert "ETH" in report

    def test_current_report_has_defi_borrow(self, sample_data):
        report = format_report(sample_data)
        assert "借贷仓位" in report
        assert "Aave V3" in report
        assert "健康度" in report

    def test_current_report_has_defi_supply(self, sample_data):
        report = format_report(sample_data)
        assert "存款/质押" in report or "DeFi" in report

    def test_current_report_nft_section(self, sample_data):
        report = format_report(sample_data)
        assert "NFT" in report
        assert "Pudgy Penguins" in report

    def test_current_no_change_line(self, sample_data):
        report = format_report(sample_data, report_type="current")
        assert "较昨日" not in report
        assert "较上周" not in report

    def test_daily_report_with_change(self, sample_data, prev_data):
        report = format_report(sample_data, prev_data=prev_data, report_type="daily")
        assert "较昨日" in report
        assert "+" in report  # net worth went up

    def test_weekly_report_with_change(self, sample_data, prev_data):
        report = format_report(sample_data, prev_data=prev_data, report_type="weekly")
        assert "较上周" in report
        # Per-asset change is attributed as a portfolio-share delta (Δ% column)
        assert "Δ%" in report
        # ...and at least one row carries a signed percentage-point delta
        import re
        assert re.search(r"[+-]\d+\.\d{2}%", report)

    def test_no_prev_data_no_crash(self, sample_data):
        report = format_report(sample_data, prev_data=None, report_type="daily")
        assert "净资产" in report
        assert "较昨日" not in report

    def test_empty_portfolio(self):
        empty = {
            "assets": {},
            "debts": {},
            "defi_positions": [],
            "total_assets": 0,
            "total_debts": 0,
            "net_worth": 0,
            "source_breakdown": {"evm": 0, "exchanges": 0, "solana": 0},
            "exchange_breakdown": {},
            "exchange_health": {},
        }
        report = format_report(empty)
        assert "净资产 $0" in report

    def test_debts_only_portfolio(self):
        data = {
            "assets": {"ETH": {"quantity": 2.0, "value": 5000.0, "price": 2500.0}},
            "debts": {"ETH": {"quantity": 3.0, "value": 7500.0, "price": 2500.0}},
            "defi_positions": [],
            "total_assets": 5000.0,
            "total_debts": 7500.0,
            "net_worth": -2500.0,
            "source_breakdown": {"evm": 5000.0, "exchanges": 0, "solana": 0},
            "exchange_breakdown": {},
            "exchange_health": {},
        }
        report = format_report(data)
        # Net asset qty (2 - 3 = -1) <= 0, so ETH should NOT appear in top assets
        assert "主要持仓" in report

    def test_report_output_is_string(self, sample_data):
        report = format_report(sample_data)
        assert isinstance(report, str)
        assert len(report) > 100

    def test_solana_defi_in_report(self, sample_data):
        report = format_report(sample_data)
        assert "Kamino" in report

    def test_allocation_section_present(self, sample_data):
        from portfolio_tracker.report_formatter import format_report
        jlp = {"SOL": 0.5, "ETH": 0.2, "BTC": 0.1, "STABLE": 0.2}
        report = format_report(sample_data, jlp_weights=jlp)
        assert "仓位占比" in report
        assert report.index("仓位占比") < report.index("主要持仓")
        assert "Stable" in report and "Alt" in report
        import re
        assert re.search(r"\bBTC\b\s+\$[\d.,]+[KM]?\s+\d+\.\d%", report)

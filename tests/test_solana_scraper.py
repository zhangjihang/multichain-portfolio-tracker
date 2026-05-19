"""Tests for Solana scraper page parsing."""

from decimal import Decimal
from pathlib import Path

from portfolio_tracker.adapters.solana_scraper import SolanaScraper


FIXTURE = Path(__file__).parent / "fixtures" / "jup_holdings_page.txt"


def test_parse_page_extracts_holdings_with_pnl_column():
    """jup.ag added a 'PnL (all time)' column between Price and Value, splitting
    the table header across two lines. The parser must still find the holdings table.
    """
    text = FIXTURE.read_text()
    result = SolanaScraper()._parse_page(text)

    holdings = {h["symbol"]: h for h in result["holdings"]}

    assert "JLP" in holdings, "spot tokens table parsing regressed"
    assert holdings["JLP"]["quantity"] > Decimal("0")
    assert holdings["JLP"]["value"] > Decimal("0")

    for sym in ("JupSOL", "CARDS", "SOL"):
        assert sym in holdings, f"missing {sym}"
        assert holdings[sym]["value"] > Decimal("0")

    total = sum(h["value"] for h in result["holdings"])
    assert total > Decimal("0"), f"holdings total not parsed: {total}"

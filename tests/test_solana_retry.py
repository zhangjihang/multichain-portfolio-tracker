"""scrape_portfolio hard-recovery: rebuild the browser between attempts
instead of silently accepting $0.
"""

import asyncio
from decimal import Decimal

from portfolio_tracker.adapters.solana_scraper import SolanaScraper

GOOD = {
    "net_worth": Decimal("123456"), "holdings": [{}],
    "defi_positions": [{}], "total_assets": Decimal("123456"),
    "total_debts": Decimal("0"),
}


def _make(scrape_results):
    """Scraper whose _scrape_once yields the given sequence; counts close()."""
    s = SolanaScraper()
    s._RETRY_BACKOFF = (0, 0, 0, 0)
    seq = iter(scrape_results)
    s.closes = 0

    async def fake_once(addr):
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    async def fake_close():
        s.closes += 1

    s._scrape_once = fake_once
    s.close = fake_close
    return s


def _run(coro):
    # _RETRY_BACKOFF is (0,0,0,0) so asyncio.sleep(0) is instant.
    return asyncio.run(coro)


def test_recovers_after_transient_failure():
    s = _make([None, None, GOOD])
    r = _run(s.scrape_portfolio("TestSolWallet11111111111111111111111111111"))
    assert r["net_worth"] == Decimal("123456")
    # browser rebuilt before each of the 2 retries
    assert s.closes == 2


def test_exception_then_success_recovers():
    s = _make([RuntimeError("wedged browser"), GOOD])
    r = _run(s.scrape_portfolio("TestSolWallet11111111111111111111111111111"))
    assert r["net_worth"] == Decimal("123456")
    assert s.closes == 1


def test_sustained_failure_returns_empty_after_all_attempts():
    s = _make([None] * 5)  # max_attempts = len(_RETRY_BACKOFF)+1 = 5
    r = _run(s.scrape_portfolio("TestSolWallet11111111111111111111111111111"))
    assert r["net_worth"] == Decimal("0")
    assert r["holdings"] == [] and r["defi_positions"] == []
    # rebuilt between every attempt (4 times), not on the last
    assert s.closes == 4

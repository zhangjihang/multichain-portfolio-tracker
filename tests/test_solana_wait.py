"""Condition-based readiness for the Solana scraper.

Root cause of recurring Solana data loss: a fixed asyncio.sleep(5) captured
the jup.ag page before its data finished loading (heavy wallets settle at
~10s on a cold browser), so net worth was $0 -> wallet silently dropped.

_parsed_ready decides, from two consecutive parses, whether the page has
actually settled (non-zero net worth AND stable across polls).
"""

from decimal import Decimal

from portfolio_tracker.adapters.solana_scraper import _parsed_ready


def _p(nw, ndefi=0):
    return {"net_worth": Decimal(str(nw)),
            "defi_positions": [{}] * ndefi, "holdings": []}


def test_zero_net_worth_never_ready():
    assert _parsed_ready(None, _p(0)) is False
    assert _parsed_ready(_p(0), _p(0)) is False
    assert _parsed_ready(_p(500, 3), _p(0)) is False


def test_first_nonzero_not_ready_until_confirmed():
    # one good reading isn't enough; could still be mid-render
    assert _parsed_ready(None, _p(100000, 4)) is False


def test_stable_nonzero_is_ready():
    assert _parsed_ready(_p(100000, 4), _p(100000, 4)) is True


def test_changing_net_worth_not_ready():
    # value still climbing as sections load in
    assert _parsed_ready(_p(60000, 1), _p(100000, 4)) is False


def test_defi_count_still_growing_not_ready():
    # net worth matched but DeFi sections still appearing
    assert _parsed_ready(_p(100000, 2), _p(100000, 4)) is False

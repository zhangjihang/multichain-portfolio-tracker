"""Regression tests for Solana value-only DeFi position handling.

jup.ag changed Kamino from itemized Lending (supply tokens) to an aggregate
"Farming/Deposit" with only a position value and no supply rows. The service
merge only counted supply/borrow/rewards tokens, so these value-only positions
were silently dropped from source_breakdown['solana'] AND from all_defi
(gated on `supply or borrow`), under-reporting Solana by ~$261k and making
the Kamino position vanish from the report's DeFi section.

Fix: _normalize_solana_position synthesizes one supply row for value-only
positions so the existing supply path counts and renders them.
"""

from decimal import Decimal
from pathlib import Path

from portfolio_tracker.service import _normalize_solana_position
from portfolio_tracker.adapters.solana_scraper import SolanaScraper

FIXTURE = Path(__file__).parent / "fixtures" / "jup_kamino_deposit_page.txt"
FIXTURE_STAKED = Path(__file__).parent / "fixtures" / "jup_kamino_staked_page.txt"


def test_value_only_deposit_gets_synthetic_supply():
    pos = {
        "protocol": "Kamino", "type": "deposit", "name": "Deposit",
        "health_rate": None, "supply": [], "borrow": [], "rewards": [],
        "value": Decimal("1000.00"),
    }
    out = _normalize_solana_position(pos)
    assert len(out["supply"]) == 1
    s = out["supply"][0]
    assert s["value"] == Decimal("1000.00")
    # quantity/price absent or zero is fine; value is what feeds totals
    assert Decimal(str(s.get("quantity", 0))) == Decimal("0")


def test_lending_with_supply_is_untouched_no_double_count():
    pos = {
        "protocol": "Kamino", "type": "lending", "name": "Main Market",
        "health_rate": 0.51, "borrow": [], "rewards": [],
        "supply": [{"symbol": "wSOL", "quantity": Decimal("3116"),
                    "price": Decimal("2.0"), "value": Decimal("500.00")}],
        "value": Decimal("500.00"),
    }
    out = _normalize_solana_position(pos)
    assert out["supply"] == pos["supply"]  # unchanged, no synthetic row added


def test_staking_with_supply_is_untouched():
    pos = {
        "protocol": "Jupiter DAO", "type": "staking", "name": "Staked",
        "health_rate": None, "borrow": [], "rewards": [],
        "supply": [{"symbol": "JUP", "quantity": Decimal("61299"),
                    "price": Decimal("0.20"), "value": Decimal("250.00")}],
        "value": Decimal("250.00"),
    }
    out = _normalize_solana_position(pos)
    assert len(out["supply"]) == 1
    assert out["supply"][0]["symbol"] == "JUP"


def test_zero_value_only_position_not_injected():
    pos = {
        "protocol": "Parcl", "type": "deposit", "name": "Deposit",
        "health_rate": None, "supply": [], "borrow": [], "rewards": [],
        "value": Decimal("0"),
    }
    out = _normalize_solana_position(pos)
    assert out["supply"] == []  # nothing to count, no synthetic row


def test_real_jup_page_kamino_deposit_resolves_token_level():
    """Kamino Deposit must parse real tokens (PYUSD...), not an aggregate row.

    Regression: the report showed a bare "Deposit"/"Staked" token because the
    scraper only captured the aggregate value; the synthetic-supply fallback
    then used the position name as the symbol.
    """
    text = FIXTURE.read_text()
    parsed = SolanaScraper()._parse_page(text)

    kamino = [p for p in parsed["defi_positions"]
              if p["protocol"] == "Kamino" and p["type"] == "deposit"]
    assert kamino, "scraper should yield a Kamino deposit position"
    pos = kamino[0]

    syms = {s["symbol"]: s for s in pos.get("supply", [])}
    # Real underlying tokens, NOT the position name "Deposit"
    assert "Deposit" not in syms
    assert "PYUSD" in syms, f"got symbols {list(syms)}"
    assert syms["PYUSD"]["value"] > Decimal("0")
    # PYUSD is the dominant Kamino deposit token
    assert syms["PYUSD"]["value"] == max(s["value"] for s in pos["supply"])
    # other Kamino tokens present
    assert {"KMNO", "WLFI", "JTO"} & set(syms)
    assert sum(s["value"] for s in pos["supply"]) > Decimal("0")


def test_real_jup_page_jupiter_dao_staked_resolves_jup():
    """Jupiter DAO Staked must resolve symbol JUP, not 'Staked'."""
    text = FIXTURE.read_text()
    parsed = SolanaScraper()._parse_page(text)

    staked = [p for p in parsed["defi_positions"]
              if p["protocol"] == "Jupiter DAO" and p["type"] == "staking"]
    assert staked, "scraper should yield a Jupiter DAO staking position"
    syms = {s["symbol"]: s for s in staked[0].get("supply", [])}
    assert "Staked" not in syms
    assert "JUP" in syms, f"got symbols {list(syms)}"
    assert syms["JUP"]["value"] > Decimal("0")


def test_real_jup_page_kamino_staked_resolves_kmno():
    """Kamino Staked uses a table layout (no Locked/Unlocked marker). It must
    resolve the real token (KMNO), not fall back to a synthetic 'Staked' row.
    """
    text = FIXTURE_STAKED.read_text()
    parsed = SolanaScraper()._parse_page(text)

    staked = [p for p in parsed["defi_positions"]
              if p["protocol"] == "Kamino" and p["type"] == "staking"]
    assert staked, "scraper should yield a Kamino staking position"
    syms = {s["symbol"]: s for s in staked[0].get("supply", [])}
    assert "Staked" not in syms
    assert "KMNO" in syms, f"got symbols {list(syms)}"
    assert syms["KMNO"]["value"] > Decimal("0")

    # After this, normalization is a no-op (real supply present)
    norm = _normalize_solana_position(staked[0])
    assert norm["supply"] == staked[0]["supply"]


def test_normalize_is_noop_when_real_supply_parsed():
    """With token-level parsing fixed, normalization must not inject a row."""
    text = FIXTURE.read_text()
    parsed = SolanaScraper()._parse_page(text)
    for p in parsed["defi_positions"]:
        if p["protocol"] in ("Kamino", "Jupiter DAO") and p.get("supply"):
            out = _normalize_solana_position(p)
            assert out["supply"] == p["supply"]  # untouched, real tokens kept


def test_parse_page_totals_no_double_count():
    """deposit/staking positions that parsed token-level supply rows must NOT
    also add the aggregate pos['value'] (that double-counted total_assets)."""
    from decimal import Decimal as D
    for fx in (FIXTURE, FIXTURE_STAKED):
        r = SolanaScraper()._parse_page(fx.read_text())
        VALUE_TYPES = ("staking", "farming", "deposit", "airdrop", "rewards")
        expected = sum(h["value"] for h in r["holdings"])
        for p in r["defi_positions"]:
            expected += sum(s.get("value", D("0")) for s in p.get("supply", []))
            if p["type"] in VALUE_TYPES and not p.get("supply"):
                expected += p.get("value", D("0"))
        assert r["total_assets"] == expected, (
            f"{fx.name}: total_assets {r['total_assets']} != {expected} "
            "(aggregate value double-counted on top of parsed supply rows)"
        )

"""Allocation classifier + aggregator (position-breakdown feature)."""

from portfolio_tracker.allocation import classify_symbol, compute_allocation


def test_mainstream_symbols():
    assert classify_symbol("BTC") == "BTC"
    assert classify_symbol("ETH") == "ETH"
    assert classify_symbol("SOL") == "SOL"
    assert classify_symbol("BNB") == "BNB"


def test_jupsol_is_sol():
    assert classify_symbol("JupSOL") == "SOL"


def test_usd_is_stable():
    assert classify_symbol("USD") == "STABLE"


def test_susdat_is_stable():
    assert classify_symbol("sUSDat") == "STABLE"


def test_stable_pt_is_stable():
    assert classify_symbol("PT-USDat-27AUG2026") == "STABLE"
    assert classify_symbol("PT-sUSDat-27AUG2026") == "STABLE"
    assert classify_symbol("PT-USDe-30OCT2025") == "STABLE"


def test_yt_is_not_stable():
    assert classify_symbol("YT-sUSDat-27AUG2026") == "ALT"


def test_jlp_marker():
    assert classify_symbol("JLP") == "JLP"


def test_altcoins():
    assert classify_symbol("ASTER") == "ALT"
    assert classify_symbol("wQUIL") == "ALT"


def test_direct_stable_yield_tokens():
    assert classify_symbol("USDat") == "STABLE"
    assert classify_symbol("USDe") == "STABLE"
    assert classify_symbol("sUSDe") == "STABLE"


def test_compute_allocation_basic_and_jlp_split():
    net_items = [
        ("BTC", {"value": 500.0}),
        ("ETH", {"value": 300.0}),
        ("JupSOL", {"value": 100.0}),
        ("USD", {"value": 200.0}),
        ("PT-USDat-27AUG2026", {"value": 150.0}),
        ("ASTER", {"value": 50.0}),
        ("JLP", {"value": 1000.0}),
        ("YT-sUSDat-27AUG2026", {"value": 10.0}),
    ]
    jlp = {"SOL": 0.5, "ETH": 0.2, "BTC": 0.1, "STABLE": 0.2}
    out = dict((c, (v, p)) for c, v, p in compute_allocation(net_items, jlp))

    total = 500 + 300 + 100 + 200 + 150 + 50 + 1000 + 10  # 2310
    assert out["BTC"][0] == 500 + 1000 * 0.1               # 600
    assert out["ETH"][0] == 300 + 1000 * 0.2               # 500
    assert out["SOL"][0] == 100 + 1000 * 0.5               # 600
    assert out["STABLE"][0] == 200 + 150 + 1000 * 0.2      # 550
    assert out["ALT"][0] == 50 + 10                        # 60
    assert abs(sum(p for _, p in out.values()) - 100.0) < 0.01
    assert abs(out["BTC"][1] - 600 / total * 100) < 1e-6
    assert "BNB" not in out


def test_compute_allocation_with_debt_negative_value():
    net_items = [("ETH", {"value": 1000.0}), ("USD", {"value": -200.0})]
    out = dict((c, (v, p)) for c, v, p in compute_allocation(net_items, None))
    assert out["ETH"][0] == 1000.0
    assert out["STABLE"][0] == -200.0
    assert abs(out["ETH"][1] - 1000 / 800 * 100) < 1e-6


def test_jlp_weights_none_falls_to_alt():
    net_items = [("JLP", {"value": 1000.0}), ("BTC", {"value": 1000.0})]
    out = dict((c, (v, p)) for c, v, p in compute_allocation(net_items, None))
    assert out["ALT"][0] == 1000.0
    assert out["BTC"][0] == 1000.0
    assert "SOL" not in out


def test_fixed_order_and_zero_skipped():
    net_items = [("ETH", {"value": 10.0}), ("BTC", {"value": 20.0})]
    cats = [c for c, _, _ in compute_allocation(net_items, None)]
    assert cats == ["BTC", "ETH"]


from portfolio_tracker.allocation import describe_allocation


def test_stable_is_last_in_order():
    net_items = [
        ("BTC", {"value": 100.0}),
        ("USD", {"value": 100.0}),
        ("ASTER", {"value": 100.0}),
    ]
    cats = [c for c, _, _ in compute_allocation(net_items, None)]
    assert cats == ["BTC", "ALT", "STABLE"]  # STABLE last, after ALT


def test_describe_allocation_dynamic():
    base = [("BTC", {"value": 100.0}), ("USD", {"value": 50.0})]
    # no JLP, no reclassified stable beyond plain USD -> no note
    assert describe_allocation(base, None) is None

    with_pt = base + [("PT-sUSDat-27AUG2026", {"value": 500.0}),
                      ("sUSDat", {"value": 10.0})]
    note = describe_allocation(with_pt, {"SOL": 1.0})
    assert "PT-sUSDat" in note and "27AUG2026" not in note  # expiry trimmed
    assert "sUSDat" in note
    assert "JLP" not in note  # no JLP position present

    jlp_w = base + [("JLP", {"value": 1000.0})]
    assert "JLP 按池成分拆入" in describe_allocation(jlp_w, {"SOL": 1.0})
    assert "整体计入 Alt" in describe_allocation(jlp_w, None)

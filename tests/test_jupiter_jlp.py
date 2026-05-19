"""JLP composition weights parsing (position-breakdown feature)."""

import json
from pathlib import Path

from portfolio_tracker.adapters.jupiter_jlp import _parse_jlp_weights

FIXTURE = Path(__file__).parent / "fixtures" / "jlp_info.json"


def test_parse_jlp_weights_from_real_response():
    payload = json.loads(FIXTURE.read_text())
    w = _parse_jlp_weights(payload)

    assert set(w) == {"SOL", "ETH", "BTC", "STABLE"}
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert abs(w["BTC"] - 16.29 / 99.98) < 1e-4
    assert abs(w["STABLE"] - (30.08 + 0.0) / 99.98) < 1e-4
    assert abs(w["SOL"] - 46.42 / 99.98) < 1e-4
    assert abs(w["ETH"] - 7.19 / 99.98) < 1e-4


def test_parse_jlp_weights_empty_returns_none():
    assert _parse_jlp_weights({"custodies": []}) is None
    assert _parse_jlp_weights({}) is None

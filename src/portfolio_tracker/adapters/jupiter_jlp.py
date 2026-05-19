"""Jupiter JLP pool composition (for position-allocation breakdown).

GET https://perps-api.jup.ag/v1/jlp-info -> custodies[].symbol +
currentWeightagePct. WBTC->BTC, USDC/USDT->STABLE, SOL->SOL, ETH->ETH.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

_JLP_INFO_URL = "https://perps-api.jup.ag/v1/jlp-info"

# custody symbol -> allocation category
_CUSTODY_CAT = {
    "SOL": "SOL",
    "ETH": "ETH",
    "WBTC": "BTC",
    "BTC": "BTC",
    "USDC": "STABLE",
    "USDT": "STABLE",
}


def _parse_jlp_weights(payload: dict) -> dict[str, float] | None:
    """Normalise jlp-info custodies into category weight fractions (sum 1)."""
    custodies = payload.get("custodies") or []
    cat_pct: dict[str, float] = {}
    total = 0.0
    for c in custodies:
        cat = _CUSTODY_CAT.get((c.get("symbol") or "").upper())
        if not cat:
            continue
        try:
            pct = float(c.get("currentWeightagePct", 0) or 0)
        except (TypeError, ValueError):
            continue  # skip a custody with a malformed weight
        cat_pct[cat] = cat_pct.get(cat, 0.0) + pct
        total += pct
    if total <= 0:
        return None
    return {cat: pct / total for cat, pct in cat_pct.items()}


async def get_jlp_weights() -> dict[str, float] | None:
    """Fetch live JLP composition weights; None on any failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_JLP_INFO_URL)
            resp.raise_for_status()
            return _parse_jlp_weights(resp.json())
    except Exception:
        logger.warning("Failed to fetch JLP weights", exc_info=True)
        return None

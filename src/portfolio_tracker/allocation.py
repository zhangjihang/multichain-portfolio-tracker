"""Position-allocation classification & aggregation.

Allocation-only reclassification: does NOT mutate TOKEN_CONVERT or any
existing behaviour. Categories: BTC, ETH, SOL, BNB, STABLE, ALT.
"""

from __future__ import annotations

import re

from .adapters.debank import TOKEN_CONVERT

# Stablecoin underlying symbols, derived from TOKEN_CONVERT (avoid double
# maintenance) plus the usdat stable-yield family.
STABLE_UNDERLYINGS: set[str] = (
    {k.upper() for k, v in TOKEN_CONVERT.items() if v == "USD"}
    | {"USD", "USDAT", "SUSDAT"}
)

# Allocation-side supplement for LSTs not covered by TOKEN_CONVERT.
_ALLOC_EXTRA_MAP: dict[str, str] = {
    "JupSOL": "SOL",
}

_MAINSTREAM = {"BTC", "ETH", "SOL", "BNB"}
_PT_RE = re.compile(r"^PT-(.+)-[0-9]")


def _is_stable_underlying(u: str) -> bool:
    u = u.strip()
    if u.upper() in STABLE_UNDERLYINGS:
        return True
    return "USD" in u.upper()


def classify_symbol(symbol: str) -> str:
    """Return one of: BTC | ETH | SOL | BNB | STABLE | ALT | JLP.

    JLP is a marker handled by compute_allocation (split by pool weights).
    """
    if symbol == "JLP":
        return "JLP"
    base = _ALLOC_EXTRA_MAP.get(symbol, symbol)
    if base in _MAINSTREAM:
        return base
    # Check direct stable or stable-yield tokens (not compound tokens like PT-/YT-*)
    if "-" not in base and (base == "USD" or _is_stable_underlying(base)):
        return "STABLE"
    m = _PT_RE.match(symbol)
    if m and _is_stable_underlying(m.group(1)):
        return "STABLE"
    return "ALT"


_ORDER = ["BTC", "ETH", "SOL", "BNB", "ALT", "STABLE"]

_PT_SHORT_RE = re.compile(r"^(PT|YT)-(.+)-[0-9].*$")


def _short_sym(symbol: str) -> str:
    """PT-sUSDat-27AUG2026 -> PT-sUSDat (drop the expiry for brevity)."""
    m = _PT_SHORT_RE.match(symbol)
    return f"{m.group(1)}-{m.group(2)}" if m else symbol


def describe_allocation(
    net_items: list[tuple[str, dict]],
    jlp_weights: dict[str, float] | None,
) -> str | None:
    """Build a brief note reflecting what actually happened this run.

    Returns None when nothing noteworthy (no reclassified stables, no JLP).
    """
    stable_extra: list[str] = []
    seen: set[str] = set()
    has_jlp = False
    for sym, _ in net_items:
        cat = classify_symbol(sym)
        if cat == "JLP":
            has_jlp = True
        elif cat == "STABLE" and sym != "USD":
            s = _short_sym(sym)
            if s not in seen:
                seen.add(s)
                stable_extra.append(s)

    clauses: list[str] = []
    if stable_extra:
        shown = "、".join(stable_extra[:3])
        more = " 等" if len(stable_extra) > 3 else ""
        clauses.append(f"Stable 计入 {shown}{more} 稳定币理财")
    if has_jlp:
        if jlp_weights:
            clauses.append("JLP 按池成分拆入 BTC/ETH/SOL/Stable")
        else:
            clauses.append("JLP 权重未取到，整体计入 Alt")
    if not clauses:
        return None
    return "；".join(clauses)


def compute_allocation(
    net_items: list[tuple[str, dict]],
    jlp_weights: dict[str, float] | None,
) -> list[tuple[str, float, float]]:
    """Aggregate net_items into category (amount, pct).

    net_items: list of (symbol, {"value": float, ...}) — token net worth
    (assets - debts, NFT excluded), same source as the holdings table.
    jlp_weights: {"SOL","ETH","BTC","STABLE"} fractions (sum 1) or None.
    Returns [(category, amount, pct)] in fixed order, zero categories omitted.
    """
    buckets: dict[str, float] = {c: 0.0 for c in _ORDER}
    for sym, info in net_items:
        val = float(info["value"])
        cat = classify_symbol(sym)
        if cat == "JLP":
            if jlp_weights:
                for wcat, w in jlp_weights.items():
                    buckets[wcat] = buckets.get(wcat, 0.0) + val * w
            else:
                buckets["ALT"] += val
        else:
            buckets[cat] += val

    total = sum(buckets.values())
    out: list[tuple[str, float, float]] = []
    for c in _ORDER:
        amt = buckets[c]
        if abs(amt) < 0.01:
            continue
        pct = (amt / total * 100.0) if total else 0.0
        out.append((c, amt, pct))
    return out

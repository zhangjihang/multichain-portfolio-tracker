"""Shared report formatting for CLI and scheduled reports."""

from __future__ import annotations

import re
from decimal import Decimal

from .adapters.debank import TOKEN_CONVERT
from .allocation import compute_allocation, describe_allocation


CHAIN_NAMES = {
    "eth": "Ethereum", "arb": "Arbitrum", "bsc": "BSC", "matic": "Polygon",
    "op": "Optimism", "avax": "Avalanche", "ftm": "Fantom", "base": "Base",
    "sol": "Solana", "ape": "ApeChain", "ron": "Ronin", "blast": "Blast",
    "linea": "Linea", "scroll": "Scroll", "zksync": "zkSync",
}


def _chain_name(c: str) -> str:
    return CHAIN_NAMES.get(c, c.title() if c else "?")


def _fmt_qty(q) -> str:
    q = float(q)
    sign = "-" if q < 0 else ""
    q = abs(q)
    if q >= 1_000_000: return f"{sign}{q / 1_000_000:,.2f}M"
    if q >= 10_000: return f"{sign}{q / 1_000:,.1f}K"
    if q >= 100: return f"{sign}{q:,.1f}"
    if q >= 1: return f"{sign}{q:,.2f}"
    return f"{sign}{q:,.4f}"


def _fmt_price(p) -> str:
    p = float(p)
    if p >= 1000: return f"${p:,.0f}"
    if p >= 1: return f"${p:,.2f}"
    if p >= 0.01: return f"${p:.4f}"
    return f"${p:.6f}"


def _fmt_val(v) -> str:
    v = float(v)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000: return f"{sign}${v / 1_000_000:,.2f}M"
    if v >= 1_000: return f"{sign}${v / 1_000:,.1f}K"
    return f"{sign}${v:,.0f}"


def _fmt_usd(value) -> str:
    if value is None:
        return "N/A"
    return f"${Decimal(str(value)):,.2f}"


def _short_addr(addr: str) -> str:
    if addr and len(addr) > 10:
        return f"{addr[:6]}...{addr[-4:]}"
    return addr or ""


def _fmt_token_line(t, prefix="└"):
    sym = t.get("symbol", "?")
    amt = float(t.get("amount", t.get("quantity", 0)))
    val = float(t.get("value", 0))
    return f"  {prefix} {sym}  {_fmt_qty(amt)} → {_fmt_val(val)}"


def _group_positions(positions: list[dict]) -> dict:
    """Group positions by (protocol, chain), merge same-address entries."""
    groups: dict = {}
    for p in positions:
        key = (p["protocol"], p["chain"])
        if key not in groups:
            groups[key] = {}
        addr = p["address"] or "(unknown)"
        if addr not in groups[key]:
            groups[key][addr] = {"health_rate": p["health_rate"], "supply": [], "borrow": []}
        entry = groups[key][addr]
        entry["supply"].extend(p["supply"])
        entry["borrow"].extend(p["borrow"])
        if p["health_rate"] is not None:
            if entry["health_rate"] is None or p["health_rate"] < entry["health_rate"]:
                entry["health_rate"] = p["health_rate"]
    return groups


# Allocation category -> display label (ASCII, Discord-safe)
_ALLOC_LABEL = {"STABLE": "Stable", "ALT": "Alt"}


def format_report(data: dict, prev_data: dict | None = None, report_type: str = "current",
                   defi_deposit_min: float = 1000.0,
                   jlp_weights: dict | None = None) -> str:
    """Format a portfolio report.

    Args:
        data: dict from get_portfolio()
        prev_data: optional previous snapshot for computing changes
        report_type: 'current' | 'daily' | 'weekly'
        defi_deposit_min: minimum USD value to show DeFi deposit/stake positions

    Returns:
        Formatted report string ready for Discord posting.
    """
    min_val = 10.0

    nw = float(data["net_worth"])
    ta = float(data["total_assets"])
    td = float(data["total_debts"])
    nft_total = sum(float(i["value"]) for s, i in data.get("assets", {}).items() if s.startswith("[NFT]"))
    token_nw = nw - nft_total
    sb = data.get("source_breakdown", {})
    bd = data.get("exchange_breakdown", {})
    health = data.get("exchange_health", {})

    lines: list[str] = []

    # --- Summary ---
    lines.append(f"🪙 **代币净资产 ${token_nw:,.0f}** | 含NFT净资产 ${nw:,.0f}")
    lines.append(f"📍 EVM ${float(sb.get('evm', 0)):,.0f} | Solana ${float(sb.get('solana', 0)):,.0f} | 交易所 ${float(sb.get('exchanges', 0)):,.0f}")

    # Change line for daily/weekly
    if prev_data is not None:
        prev_nw = float(prev_data["net_worth"])
        diff = nw - prev_nw
        pct = (diff / prev_nw * 100) if prev_nw else 0
        sign = "+" if diff >= 0 else ""
        if report_type == "daily":
            lines.append(f"较昨日：{sign}${abs(diff):,.0f} ({sign}{pct:.2f}%)")
        elif report_type == "weekly":
            lines.append(f"较上周：{sign}${abs(diff):,.0f} ({sign}{pct:.2f}%)")
    lines.append("")

    # --- Exchanges ---
    lines.append("**🏛️交易所**")
    for k, v in sorted(bd.items(), key=lambda x: float(x[1].get("assets", 0)), reverse=True):
        a = float(v.get("assets", 0))
        d = float(v.get("debts", 0))
        if a < 1 and d < 1:
            continue
        line = f"  {k.title()}: ${a:,.0f}"
        if d > 0:
            h_data = health.get(k, {})
            # Use per-loan health data if available (more accurate than total assets/debts)
            if h_data.get("ltv"):
                ltv = float(h_data["ltv"])
                hr = 1.0 / ltv if ltv > 0 else 999.0
                collateral = float(h_data.get("collateral_usd", 0))
            else:
                hr = a / d if d > 0 else 999.0
                collateral = a
            # Liquidation price from main collateral
            max_ltv = 0.85
            liq_str = ""
            mc = h_data.get("main_collateral", {})
            if mc and mc.get("amount", 0) > 0:
                cur_ltv = float(h_data.get("ltv", 0)) if h_data.get("ltv") else (d / collateral if collateral > 0 else 0)
                if cur_ltv > 0:
                    sym = mc["symbol"]
                    cur_price = mc["value_usd"] / mc["amount"]
                    liq_price = cur_price * (cur_ltv / max_ltv)
                    drop_pct = (1 - cur_ltv / max_ltv) * 100
                    liq_str = f" | {sym}清算价 {_fmt_price(liq_price)} (-{drop_pct:.0f}%)"
            elif hr > 1 and hr < 100:
                drop_pct = (1 - 1 / hr) * 100
                liq_str = f" | -{drop_pct:.0f}%清算"
            line += f" (负债 ${d:,.0f} | 健康度 {hr:.2f}{liq_str})"
        lines.append(line)
    lines.append("")

    # --- Top assets table (tokens only, NFTs shown separately) ---
    # Merge derivatives (ETH/SOL/BTC/stablecoins) via TOKEN_CONVERT, subtract debts
    _TC_UPPER = {k.upper(): v for k, v in TOKEN_CONVERT.items()}

    def _merge_key(symbol: str) -> str:
        return _TC_UPPER.get(symbol.upper(), symbol)

    # Build net holdings: assets - debts, with derivative merging
    debts = data.get("debts", {})
    merged: dict[str, dict] = {}
    for s, i in data["assets"].items():
        if s.startswith("[NFT]"):
            continue
        key = _merge_key(s)
        qty = float(i["quantity"])
        val = float(i["value"])
        price = float(i.get("price", 0))
        if key != s and key in merged and merged[key]["price"] > 0:
            # Convert derivative qty to base token equivalent
            qty = val / merged[key]["price"] if merged[key]["price"] > 0 else qty
        if key in merged:
            if key == s and price > 0:
                merged[key]["price"] = price  # prefer base token price
            merged[key]["quantity"] += qty
            merged[key]["value"] += val
        else:
            merged[key] = {"quantity": qty, "value": val, "price": price}

    for s, d in debts.items():
        key = _merge_key(s)
        debt_qty = float(d.get("quantity", 0))
        debt_val = float(d.get("value", 0))
        if key in merged:
            if merged[key]["price"] > 0:
                debt_qty = debt_val / merged[key]["price"]
            merged[key]["quantity"] -= debt_qty
            merged[key]["value"] -= debt_val
        else:
            price = float(d.get("price", 0))
            merged[key] = {"quantity": -debt_qty, "value": -debt_val, "price": price}

    net_items: list[tuple[str, dict]] = []
    for s, i in merged.items():
        val = i["value"]
        if abs(val) < min_val:
            continue
        net_items.append((s, i))
    net_items.sort(key=lambda x: x[1]["value"], reverse=True)

    nft_items = [(s, i) for s, i in data["assets"].items() if s.startswith("[NFT]") and float(i["value"]) >= min_val]
    nft_items.sort(key=lambda x: x[1]["value"], reverse=True)

    alloc = compute_allocation(net_items, jlp_weights)
    if alloc:
        lines.append("**📐仓位占比**")
        arows = [(_ALLOC_LABEL.get(c, c), _fmt_val(amt), f"{pct:.1f}%")
                 for c, amt, pct in alloc]
        # "Cat"/"Value"/"%" are min-width floors, not printed headers
        aw0 = max(max(len(r[0]) for r in arows), len("Cat"))
        aw1 = max(max(len(r[1]) for r in arows), len("Value"))
        aw2 = max(max(len(r[2]) for r in arows), len("%"))
        lines.append("```")
        for c, v, p in arows:
            lines.append(f"{c:<{aw0}}  {v:>{aw1}}  {p:>{aw2}}")
        lines.append("```")
        _note = describe_allocation(net_items, jlp_weights)
        if _note:
            lines.append(f"> 说明：{_note}")
        lines.append("")

    lines.append("**📊主要持仓**")

    # Build prev merged holdings for comparison (same merge logic).
    # Used for both daily (share delta) and weekly (detailed breakdown).
    prev_merged: dict[str, dict] = {}
    prev_total_assets: float = 0.0
    if prev_data is not None:
        _prev = prev_data.get("data", prev_data) if "data" in prev_data else prev_data
        prev_total_assets = float(_prev.get("total_assets", 0) or 0)
        p_assets = _prev.get("assets", {})
        p_debts = _prev.get("debts", {})
        for s, i in p_assets.items():
            if s.startswith("[NFT]"):
                continue
            key = _merge_key(s)
            qty = float(i["quantity"] if isinstance(i, dict) else 0)
            val = float(i["value"] if isinstance(i, dict) else 0)
            price = float(i.get("price", 0) if isinstance(i, dict) else 0)
            if key != s and key in prev_merged and prev_merged[key]["price"] > 0:
                qty = val / prev_merged[key]["price"]
            if key in prev_merged:
                if key == s and price > 0:
                    prev_merged[key]["price"] = price
                prev_merged[key]["quantity"] += qty
                prev_merged[key]["value"] += val
            else:
                prev_merged[key] = {"quantity": qty, "value": val, "price": price}
        for s, d in p_debts.items():
            key = _merge_key(s)
            dq = float(d.get("quantity", 0) if isinstance(d, dict) else 0)
            dv = float(d.get("value", 0) if isinstance(d, dict) else 0)
            if key in prev_merged:
                if prev_merged[key]["price"] > 0:
                    dq = dv / prev_merged[key]["price"]
                prev_merged[key]["quantity"] -= dq
                prev_merged[key]["value"] -= dv
            else:
                price = float(d.get("price", 0) if isinstance(d, dict) else 0)
                prev_merged[key] = {"quantity": -dq, "value": -dv, "price": price}

    # Token symbol truncation for tight display
    def _short_sym(s: str, width: int = 12) -> str:
        return s if len(s) <= width else s[: width - 2] + ".."

    HOLDINGS_MIN = 500.0  # hide dust below this USD value

    rows = []
    other_val = 0.0
    other_count = 0
    for s, i in net_items:
        val = i["value"]
        if abs(val) < HOLDINGS_MIN:
            other_val += float(val)
            other_count += 1
            continue
        qty = i["quantity"]
        price = i["price"]
        pct = (val / ta * 100) if ta > 0 else 0
        pct_str = f"{pct:.1f}%"

        # Share delta vs previous snapshot (percentage points change in portfolio weight)
        delta_str = ""
        if s in prev_merged and prev_total_assets > 0:
            old_val = float(prev_merged[s]["value"])
            old_share = old_val / prev_total_assets * 100
            d_pp = pct - old_share  # percentage-point difference
            if abs(d_pp) >= 0.05:
                sign = "+" if d_pp >= 0 else ""
                delta_str = f"{sign}{d_pp:.2f}%"

        rows.append((_short_sym(s), _fmt_qty(qty), _fmt_val(val), pct_str, delta_str))

    if rows:
        # 5-column compact layout with vertical bar separators for visual alignment
        sep = " | "
        has_delta = any(r[4] for r in rows)
        w0 = max(max(len(r[0]) for r in rows), len("Token"))
        w1 = max(max(len(r[1]) for r in rows), len("Qty"))
        w2 = max(max(len(r[2]) for r in rows), len("Value"))
        w3 = max(max(len(r[3]) for r in rows), len("%"))

        # Δ% = change in portfolio share (percentage points) vs prev snapshot
        delta_label = "Δ%"

        lines.append("```")
        if has_delta:
            w4 = max(max(len(r[4]) for r in rows), len(delta_label))
            lines.append(f"{'Token':<{w0}}{sep}{'Qty':>{w1}}{sep}{'Value':>{w2}}{sep}{'%':>{w3}}{sep}{delta_label:>{w4}}")
            lines.append(f"{'-' * w0}-+-{'-' * w1}-+-{'-' * w2}-+-{'-' * w3}-+-{'-' * w4}")
            for s, q, v, pct_s, dlt in rows:
                lines.append(f"{s:<{w0}}{sep}{q:>{w1}}{sep}{v:>{w2}}{sep}{pct_s:>{w3}}{sep}{dlt:>{w4}}")
            if other_count:
                lines.append(f"{'+' + str(other_count) + ' others':<{w0}}{sep}{'':>{w1}}{sep}{_fmt_val(other_val):>{w2}}{sep}{'':>{w3}}{sep}{'':>{w4}}")
        else:
            lines.append(f"{'Token':<{w0}}{sep}{'Qty':>{w1}}{sep}{'Value':>{w2}}{sep}{'%':>{w3}}")
            lines.append(f"{'-' * w0}-+-{'-' * w1}-+-{'-' * w2}-+-{'-' * w3}")
            for s, q, v, pct_s, _ in rows:
                lines.append(f"{s:<{w0}}{sep}{q:>{w1}}{sep}{v:>{w2}}{sep}{pct_s:>{w3}}")
            if other_count:
                lines.append(f"{'+' + str(other_count) + ' others':<{w0}}{sep}{'':>{w1}}{sep}{_fmt_val(other_val):>{w2}}{sep}{'':>{w3}}")
        lines.append("```")

    # --- NFTs (one per line, in code block for clean rendering) ---
    if nft_items:
        # Start a new Discord message so code blocks don't get cut mid-section
        lines.append("<<<MSG_BREAK>>>")
        NFT_NAME_MAX = 26

        def _clean_nft_name(name: str) -> str:
            # Hex-address collection names: show shortened
            if re.fullmatch(r"0x[0-9a-fA-F]{40}", name):
                return f"Unknown [{name[2:6]}..{name[-4:]}]"
            # Collapse "G r i f t e r s" / "B E E P L E" single-letter-spaced sequences
            def _collapse(m: "re.Match") -> str:
                return m.group(0).replace(" ", "")
            name = re.sub(r"\b(?:[A-Za-z] ){2,}[A-Za-z]\b", _collapse, name)
            # Truncate long names
            if len(name) > NFT_NAME_MAX:
                name = name[: NFT_NAME_MAX - 2] + ".."
            return name

        nft_total = sum(float(i["value"]) for _, i in nft_items)
        major_nfts = [(s.removeprefix("[NFT]"), i) for s, i in nft_items if float(i["value"]) >= 500]
        minor_count = len(nft_items) - len(major_nfts)
        lines.append(f"🖼️**NFT合计 {_fmt_val(nft_total)}**")
        if major_nfts:
            nft_rows = []
            for name, i in major_nfts:
                qty = int(float(i.get("quantity", 1)))
                val = _fmt_val(float(i["value"]))
                qty_str = f" x{qty}" if qty > 1 else ""
                nft_rows.append((f"{_clean_nft_name(name)}{qty_str}", val))
            nw0 = max(len(r[0]) for r in nft_rows)
            nw1 = max(len(r[1]) for r in nft_rows)
            lines.append("```")
            for name_s, val_s in nft_rows:
                lines.append(f"{name_s:<{nw0}}   {val_s:>{nw1}}")
            lines.append("```")
        if minor_count > 0:
            minor_val = nft_total - sum(float(i["value"]) for _, i in major_nfts)
            lines.append(f"  +{minor_count}个其他 {_fmt_val(minor_val)}")
    lines.append("")

    # --- DeFi positions (grouped by protocol) ---
    defi_borrow: list[dict] = []
    defi_supply_only: list[dict] = []
    for pos in data.get("defi_positions", []):
        h = (pos.get("detail") or {}).get("health_rate")
        supply = [t for t in pos.get("supply", []) if float(t.get("value", 0)) >= min_val]
        borrow = [t for t in pos.get("borrow", []) if float(t.get("value", 0)) >= min_val]
        if not supply and not borrow:
            continue
        entry = {
            "protocol": pos.get("protocol", "?"),
            "chain": pos.get("chain", "?"),
            "name": pos.get("name", ""),
            "address": _short_addr(pos.get("address", "")),
            "health_rate": float(h) if h is not None else None,
            "supply": supply,
            "borrow": borrow,
        }
        if borrow:
            defi_borrow.append(entry)
        else:
            defi_supply_only.append(entry)

    if defi_borrow or defi_supply_only:
        # Start a new Discord message for DeFi section
        lines.append("<<<MSG_BREAK>>>")

    if defi_borrow:
        lines.append("**⚠️DeFi借贷仓位**")
        groups = _group_positions(defi_borrow)
        # Collect all rows into one code block
        all_br: list[tuple[str, str, str, str]] = []
        for gi, ((proto, chain), addrs) in enumerate(sorted(groups.items(), key=lambda x: sum(sum(float(t.get("value", 0)) for t in a["supply"]) for a in x[1].values()), reverse=True)):
            for addr, info in addrs.items():
                if all_br:
                    all_br.append(("", "", "", ""))
                hr = info["health_rate"]
                line1 = f"[{proto} / {_chain_name(chain)}] {addr}"
                all_br.append(("", line1, "", ""))
                if hr is not None:
                    drop_pct = (1 - 1 / hr) * 100 if hr > 1 else 0
                    main_supply = max(info["supply"], key=lambda t: float(t.get("value", 0))) if info["supply"] else None
                    liq_info = ""
                    if main_supply and drop_pct > 0:
                        sym = main_supply.get("symbol", "?")
                        amt = float(main_supply.get("amount", main_supply.get("quantity", 0)))
                        val = float(main_supply.get("value", 0))
                        if amt > 0:
                            liq_price = (val / amt) * (1 / hr)
                            liq_info = f"  {sym} liq {_fmt_price(liq_price)} (-{drop_pct:.0f}%)"
                        else:
                            liq_info = f"  -{drop_pct:.0f}% liq"
                    all_br.append(("", f"HR {hr:.2f}{liq_info}", "", ""))
                for t in info["supply"]:
                    sym = t.get("symbol", "?")
                    amt = float(t.get("amount", t.get("quantity", 0)))
                    val = float(t.get("value", 0))
                    all_br.append(("+", sym, _fmt_qty(amt) if amt > 0 else "", _fmt_val(val)))
                for t in info["borrow"]:
                    sym = t.get("symbol", "?")
                    amt = float(t.get("amount", t.get("quantity", 0)))
                    val = float(t.get("value", 0))
                    all_br.append(("-", sym, _fmt_qty(amt) if amt > 0 else "", _fmt_val(val)))
        if all_br:
            data_rows = [r for r in all_br if r[0] in ("+", "-")]
            sep = "  "
            bw1 = max((len(r[1]) for r in data_rows), default=4)
            bw2 = max((len(r[2]) for r in data_rows), default=3)
            bw3 = max((len(r[3]) for r in data_rows), default=5)
            lines.append("```")
            for sign, sym, q, v in all_br:
                if sign == "":
                    lines.append(sym)
                else:
                    lines.append(f"{sign} {sym:<{bw1}}{sep}{q:>{bw2}}{sep}{v:>{bw3}}")
            lines.append("```")
        lines.append("")

    if defi_supply_only:
        lines.append("**🌾DeFi存款/质押**")
        defi_supply_only = [p for p in defi_supply_only if sum(float(t.get("value", 0)) for t in p["supply"]) >= defi_deposit_min]
        groups = _group_positions(defi_supply_only)

        # Chain short label mapping
        _CHAIN_SHORT = {
            "eth": "eth", "ethereum": "eth",
            "arb": "arb", "arbitrum": "arb",
            "bsc": "bsc", "matic": "pol", "polygon": "pol",
            "op": "op", "optimism": "op",
            "avax": "avax", "avalanche": "avax",
            "base": "base",
            "sol": "sol", "solana": "sol",
            "ape": "ape", "apechain": "ape",
            "ron": "ron", "ronin": "ron",
            "blast": "blast", "linea": "linea", "scroll": "scroll",
        }

        # One row per token. Columns: Protocol, Token, Qty, Value
        # (Yield column intentionally omitted — needs a dedicated DeFi position
        # monitor to compute accurately; simple qty diffs on DeBank data weren't
        # reliable because amount is principal, not accrued balance.)
        defi_rows: list[tuple[str, str, str, str]] = []
        for (proto, chain), addrs in sorted(groups.items(), key=lambda x: sum(sum(float(t.get("value", 0)) for t in a["supply"]) for a in x[1].values()), reverse=True):
            merged_tokens: dict = {}
            for addr, info in addrs.items():
                for t in info["supply"]:
                    sym = t.get("symbol", "?")
                    amt = float(t.get("amount", t.get("quantity", 0)))
                    val = float(t.get("value", 0))
                    if sym in merged_tokens:
                        merged_tokens[sym]["amount"] += amt
                        merged_tokens[sym]["value"] += val
                    else:
                        merged_tokens[sym] = {"amount": amt, "value": val}
            tokens_sorted = sorted(merged_tokens.items(), key=lambda x: x[1]["value"], reverse=True)

            # Proto label: "Kamino/sol" (ASCII slash for reliable monospace alignment)
            chain_short = _CHAIN_SHORT.get(chain, chain[:4] if chain else "?")
            proto_label = f"{proto}/{chain_short}"

            # One row per token. Protocol label only on first row.
            for idx, (sym, tv) in enumerate(tokens_sorted):
                label = proto_label if idx == 0 else ""
                sym_short = sym if len(sym) <= 12 else sym[:10] + ".."
                qty_str = _fmt_qty(tv["amount"]) if tv["amount"] > 0 else ""
                defi_rows.append((label, sym_short, qty_str, _fmt_val(tv["value"])))

        if defi_rows:
            # Use explicit " | " separators so columns are visually locked even
            # if Discord's monospace font has subtle width inconsistencies.
            sep = " | "
            dw0 = max(max(len(r[0]) for r in defi_rows), len("Protocol"))
            dw1 = max(max(len(r[1]) for r in defi_rows), len("Token"))
            dw2 = max(max(len(r[2]) for r in defi_rows), len("Qty"))
            dw3 = max(max(len(r[3]) for r in defi_rows), len("Value"))
            lines.append("```")
            lines.append(f"{'Protocol':<{dw0}}{sep}{'Token':<{dw1}}{sep}{'Qty':>{dw2}}{sep}{'Value':>{dw3}}")
            lines.append(f"{'-' * dw0}-+-{'-' * dw1}-+-{'-' * dw2}-+-{'-' * dw3}")
            for p, t, q, v in defi_rows:
                lines.append(f"{p:<{dw0}}{sep}{t:<{dw1}}{sep}{q:>{dw2}}{sep}{v:>{dw3}}")
            lines.append("```")

    # --- Manual entries ---
    manual_entries = data.get("manual_entries", [])
    if manual_entries:
        manual_total = sum(e.get("value_usd") or 0 for e in manual_entries)
        lines.append(f"**📋手动记录 (合计 {_fmt_val(manual_total)})**")
        m_rows = []
        for e in manual_entries:
            val = e.get("value_usd")
            val_str = _fmt_val(val) if val is not None else "-"
            exp = f" [到期 {e['expires_at']}]" if e.get("expires_at") else ""
            m_rows.append((e["project"], e["coin"], _fmt_qty(e["quantity"]), val_str, exp))
        if m_rows:
            sep = "   "
            mw0 = max(len(r[0]) for r in m_rows)
            mw1 = max(len(r[1]) for r in m_rows)
            mw2 = max(len(r[2]) for r in m_rows)
            mw3 = max(len(r[3]) for r in m_rows)
            lines.append("```")
            for proj, coin, qty, val_s, exp in m_rows:
                lines.append(f"{proj:<{mw0}}{sep}{coin:<{mw1}}{sep}{qty:>{mw2}}{sep}{val_s:>{mw3}}{exp}")
            lines.append("```")
        lines.append("")

    # --- DeBank API usage ---
    debank_units = data.get("debank_units")
    if debank_units is not None:
        used = int(debank_units.get("today_usage", 0))
        remaining = int(debank_units.get("balance", 0))
        # Estimate remaining days based on today's usage
        est_days = ""
        if used > 0:
            days_left = remaining / used
            est_days = f" | 预计可用 {days_left:.0f} 天"
        lines.append("")
        lines.append(f"📊 DeBank API 今日用量: {used:,} units | 剩余 {remaining:,} units{est_days}")

    return "\n".join(lines)

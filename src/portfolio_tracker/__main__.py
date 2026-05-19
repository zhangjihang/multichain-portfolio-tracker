"""CLI entry point: python -m portfolio_tracker <command>"""

import asyncio
import json
import sys
import logging
import time
from decimal import Decimal
from pathlib import Path

from .adapters.jupiter_jlp import get_jlp_weights
from .config import load_config
from .report_formatter import format_report
from .service import PortfolioService
from .storage import SnapshotStorage
from .utils.keychain import get_secret, set_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _update_config_addresses(chain: str, address: str, add: bool):
    """Add or remove an address from config.yaml."""
    import yaml
    config_path = None
    for candidate in ["config.yaml", "config.yml"]:
        if Path(candidate).exists():
            config_path = Path(candidate)
            break
    if not config_path:
        print("config.yaml not found", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    key = "evm_addresses" if chain == "evm" else "solana_addresses"
    addresses = data.get(key, [])

    if add:
        # Case-insensitive check for EVM
        existing = {a.lower() for a in addresses} if chain == "evm" else set(addresses)
        check = address.lower() if chain == "evm" else address
        if check in existing:
            print(f"Address already exists: {address}")
            return
        addresses.append(address)
        data[key] = addresses
        print(f"Added {chain} address: {address}")
    else:
        if chain == "evm":
            new_list = [a for a in addresses if a.lower() != address.lower()]
        else:
            new_list = [a for a in addresses if a != address]
        if len(new_list) == len(addresses):
            print(f"Address not found: {address}")
            return
        data[key] = new_list
        print(f"Removed {chain} address: {address}")

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


async def _run(cmd, args):
    config = load_config()
    service = PortfolioService(config)
    db_path = getattr(config, "db_path", None) or "./data/portfolio.db"
    storage = SnapshotStorage(db_path)

    if cmd == "snapshot":
        data = await service.get_portfolio(force_refresh=True)
        data["timestamp"] = float(data.get("timestamp", 0))
        await storage.save_snapshot(data)
        print(json.dumps(data, default=_json_default, indent=2))

    elif cmd == "portfolio":
        data = await service.get_portfolio()
        out = {
            "net_worth": float(data["net_worth"]),
            "total_assets": float(data["total_assets"]),
            "total_debts": float(data["total_debts"]),
            "source_breakdown": {k: float(v) for k, v in data.get("source_breakdown", {}).items()},
            "exchange_breakdown": {
                k: {kk: float(vv) for kk, vv in v.items()}
                for k, v in data.get("exchange_breakdown", {}).items()
            },
        }
        print(json.dumps(out, indent=2))

    elif cmd == "assets":
        show_all = "--all" in args
        min_val = 10.0
        data = await service.get_portfolio()
        items = [(s, i) for s, i in data["assets"].items() if float(i["value"]) >= min_val]
        items.sort(key=lambda x: x[1]["value"], reverse=True)
        if not show_all:
            items = items[:15]
        out = [
            {"symbol": s, "quantity": float(i["quantity"]), "price": float(i.get("price", 0)), "value": float(i["value"])}
            for s, i in items
        ]
        print(json.dumps(out, indent=2))

    elif cmd == "defi":
        min_val = 10.0
        data = await service.get_portfolio()
        positions = data.get("defi_positions", [])
        out = []
        for pos in positions:
            h = (pos.get("detail") or {}).get("health_rate")
            supply = [t for t in pos.get("supply", []) if float(t.get("value", 0)) >= min_val]
            borrow = [t for t in pos.get("borrow", []) if float(t.get("value", 0)) >= min_val]
            if not supply and not borrow:
                continue
            out.append({
                "protocol": pos.get("protocol"),
                "chain": pos.get("chain"),
                "name": pos.get("name"),
                "address": pos.get("address", "")[:6] + "..." + pos.get("address", "")[-4:] if pos.get("address") else "",
                "health_rate": float(h) if h is not None else None,
                "supply": [{"symbol": t.get("symbol"), "amount": float(t.get("amount", 0)), "value": float(t.get("value", 0))} for t in supply],
                "borrow": [{"symbol": t.get("symbol"), "amount": float(t.get("amount", 0)), "value": float(t.get("value", 0))} for t in borrow],
            })
        print(json.dumps(out, indent=2))

    elif cmd == "exchange":
        data = await service.get_portfolio()
        bd = data.get("exchange_breakdown", {})
        health = data.get("exchange_health", {})
        out = {}
        for k, v in bd.items():
            entry = {"assets": float(v.get("assets", 0)), "debts": float(v.get("debts", 0)), "net": float(v.get("assets", 0) - v.get("debts", 0))}
            if k in health:
                entry["loan_health"] = health[k]
            out[k] = entry
        print(json.dumps(out, indent=2))

    elif cmd == "report":
        """Pre-formatted report ready to post to Discord."""
        data = await service.get_portfolio()
        data["timestamp"] = float(data.get("timestamp", 0))
        await storage.save_snapshot(data)
        manual = await storage.get_manual_entries(active_only=True)
        if manual:
            data["manual_entries"] = manual
        jlp_w = await get_jlp_weights()
        print(format_report(data, jlp_weights=jlp_w))

    elif cmd == "wallets":
        min_value = float(args[0]) if args else 10.0
        data = await service.get_portfolio()
        out = []
        for w in data.get("wallet_breakdown", []):
            addr = w["address"]
            short_addr = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr
            assets = [a for a in w["assets"] if float(a.get("value", 0)) >= min_value]
            assets.sort(key=lambda x: float(x["value"]), reverse=True)
            debts = [d for d in w["debts"] if float(d.get("value", 0)) >= min_value]
            defi_filtered = []
            for d in w.get("defi", []):
                supply = [t for t in d.get("supply", []) if float(t.get("value", 0)) >= min_value]
                borrow = [t for t in d.get("borrow", []) if float(t.get("value", 0)) >= min_value]
                if supply or borrow:
                    defi_filtered.append({
                        "protocol": d.get("protocol"),
                        "chain": d.get("chain"),
                        "name": d.get("name"),
                        "health_rate": float(d["detail"]["health_rate"]) if d.get("detail", {}).get("health_rate") is not None else None,
                        "supply": [{"symbol": t["symbol"], "quantity": float(t["amount"]), "value": float(t["value"])} for t in supply],
                        "borrow": [{"symbol": t["symbol"], "quantity": float(t["amount"]), "value": float(t["value"])} for t in borrow],
                    })
            if assets or debts or defi_filtered:
                out.append({
                    "address": short_addr,
                    "source": w.get("source", "evm"),
                    "total": float(w["total"]),
                    "assets": [{"symbol": a["symbol"], "quantity": float(a["quantity"]), "price": float(a["price"]), "value": float(a["value"])} for a in assets],
                    "debts": [{"symbol": d["symbol"], "quantity": float(d["quantity"]), "price": float(d["price"]), "value": float(d["value"])} for d in debts],
                    "defi": defi_filtered,
                })
        out.sort(key=lambda x: x["total"], reverse=True)
        print(json.dumps(out, indent=2))

    elif cmd == "history":
        days = int(args[0]) if args else 7
        history = await storage.get_history(days)
        current = await service.get_portfolio()
        net_now = float(current["net_worth"])
        out = {"current": net_now, "snapshots": []}
        for h in history:
            out["snapshots"].append({
                "timestamp": h.get("timestamp"),
                "net_worth": float(h["net_worth"]),
                "total_assets": float(h["total_assets"]),
                "total_debts": float(h["total_debts"]),
            })
        print(json.dumps(out, indent=2))

    elif cmd == "preview":
        """Render report from latest saved snapshot (no API calls)."""
        report_type = args[0] if args else "current"
        latest = await storage.get_latest()
        if not latest:
            print("No snapshots found. Run 'snapshot' or 'report' first.")
            sys.exit(1)
        snap_data = latest.get("data", latest)
        manual = await storage.get_manual_entries(active_only=True)
        if manual:
            snap_data["manual_entries"] = manual
        prev = None
        now = time.time()
        if report_type == "weekly":
            prev = await storage.get_nearest(now - 7 * 86400, exclude_after=now - 3600)
        elif report_type == "daily":
            prev = await storage.get_nearest(now - 86400, exclude_after=now - 3600)
        print(format_report(snap_data, prev_data=prev, report_type=report_type))

    elif cmd == "weekly-report":
        data = await service.get_portfolio()
        data["timestamp"] = float(data.get("timestamp", 0))
        await storage.save_snapshot(data)
        manual = await storage.get_manual_entries(active_only=True)
        if manual:
            data["manual_entries"] = manual
        now = time.time()
        prev = await storage.get_nearest(now - 7 * 86400, exclude_after=now - 3600)
        jlp_w = await get_jlp_weights()
        print(format_report(data, prev_data=prev, report_type="weekly", jlp_weights=jlp_w))

    elif cmd == "check-health":
        threshold = float(args[0]) if args else 1.5
        min_val = 10.0  # ignore tiny positions
        # Use lightweight health-only fetch (skips DeBank wallet tokens)
        data = await service.get_health_data()
        alerts = []
        for pos in data.get("defi_positions", []):
            h = (pos.get("detail") or {}).get("health_rate")
            if h is not None and float(h) < threshold:
                supply_val = sum(float(t.get("value", 0)) for t in pos.get("supply", []))
                borrow_val = sum(float(t.get("value", 0)) for t in pos.get("borrow", []))
                if supply_val < min_val and borrow_val < min_val:
                    continue
                alerts.append({
                    "protocol": pos.get("protocol"),
                    "chain": pos.get("chain"),
                    "name": pos.get("name"),
                    "health_rate": float(h),
                    "threshold": threshold,
                    "address": pos.get("address", "")[:6] + "..." + pos.get("address", "")[-4:] if pos.get("address") else "",
                    "supply_usd": supply_val,
                    "borrow_usd": borrow_val,
                })
        # Include exchange loan health in output
        exchange_health = data.get("exchange_health", {})
        for ex_name, ex_data in exchange_health.items():
            ltv = ex_data.get("ltv", 0)
            # Alert if LTV > 0.7 (70%)
            if ltv > 0.7:
                alerts.append({
                    "protocol": f"{ex_name} crypto loan",
                    "chain": "cex",
                    "name": "loan",
                    "health_rate": round(1 / ltv, 2) if ltv > 0 else 999,
                    "threshold": threshold,
                    "address": ex_name,
                    "supply_usd": ex_data.get("collateral_usd", 0),
                    "borrow_usd": ex_data.get("debt_usd", 0),
                })
        cache_status = data.get("cache_status", "ok")
        if cache_status != "ok":
            alerts.append({
                "protocol": "system",
                "chain": "cache",
                "name": f"borrow_cache_{cache_status}",
                "health_rate": 0,
                "threshold": 0,
                "address": "",
                "supply_usd": 0,
                "borrow_usd": 0,
                "message": f"Borrow address cache is {cache_status}. Full portfolio fetch may not be running properly.",
            })
        # Check expiring manual entries (folds check-expiry into check-health)
        expiring = await storage.get_expiring_entries()
        for e in expiring:
            alerts.append({
                "protocol": "manual_entry",
                "chain": "manual",
                "name": e.get("project", ""),
                "health_rate": 0,
                "threshold": 0,
                "address": "",
                "supply_usd": float(e.get("value_usd", 0) or 0),
                "borrow_usd": 0,
                "message": f"⏰ 到期提醒: [{e['project']}] {e['coin']} x {e['quantity']}"
                           + (f" — {e['notes']}" if e.get('notes') else ""),
                "entry_id": e["id"],
            })
            await storage.mark_reminded(e["id"])

        print(json.dumps({"alerts": alerts, "count": len(alerts), "exchange_health": exchange_health}, indent=2))

    elif cmd == "breakdown":
        """Per-source asset breakdown from latest snapshot (no API calls)."""
        min_value = float(args[0]) if args else 100.0
        latest = await storage.get_latest()
        if not latest:
            print("No snapshots found. Run 'report' first.")
            sys.exit(1)
        snap = latest.get("data", latest)

        sep = "   "

        # Group wallets by source, sorted by net value within each
        wallets = snap.get("wallet_breakdown", [])
        groups: dict[str, list] = {}
        for w in wallets:
            groups.setdefault(w.get("source", "evm"), []).append(w)
        for src in groups:
            groups[src].sort(key=lambda w: float(w.get("total", 0)), reverse=True)

        for section, label in [("exchange", "EXCHANGES"), ("evm", "EVM WALLETS"), ("solana", "SOLANA WALLETS")]:
            section_wallets = groups.get(section, [])
            if not section_wallets:
                continue
            section_total = sum(float(w.get("total", 0)) for w in section_wallets)
            print(f"\n{'#' * 50}")
            print(f"  {label}  (total: ${section_total:,.0f})")
            print(f"{'#' * 50}")

            from portfolio_tracker.report_formatter import _fmt_qty, _fmt_val

            for w in section_wallets:
                addr = w.get("address", "")
                source = w.get("source", "evm")
                total = float(w.get("total", 0))
                assets = [a for a in w.get("assets", []) if float(a.get("value", 0)) >= min_value]
                assets.sort(key=lambda x: -float(x["value"]))
                debts = [d for d in w.get("debts", []) if float(d.get("value", 0)) >= min_value]
                defi = w.get("defi", [])

                if source == "exchange":
                    wname = addr.upper()
                elif len(addr) > 10:
                    wname = f"{addr[:6]}...{addr[-4:]}"
                else:
                    wname = addr

                # Header
                print(f"\n  {wname}  ${total:,.0f}")

                # Token table (assets + debts)
                token_rows: list[tuple[str, str, str]] = []
                for a in assets:
                    token_rows.append((a["symbol"], _fmt_qty(float(a.get("quantity", 0))), _fmt_val(float(a["value"]))))
                for d in debts:
                    token_rows.append((d["symbol"], f"-{_fmt_qty(float(d.get('quantity', 0)))}", f"-{_fmt_val(float(d['value']))}"))

                if token_rows:
                    tw0 = max(len(r[0]) for r in token_rows)
                    tw1 = max(len(r[1]) for r in token_rows)
                    tw2 = max(len(r[2]) for r in token_rows)
                    for tok, q, v in token_rows:
                        print(f"    {tok:<{tw0}}{sep}{q:>{tw1}}{sep}{v:>{tw2}}")

                # DeFi positions (each as a separate block under the address)
                defi_filtered = [d for d in defi if d.get("supply") or d.get("borrow")]
                for d in defi_filtered:
                    proto = d.get("protocol", "?")
                    dname = d.get("name", "")
                    hr = d.get("health_rate") or (d.get("detail", {}) or {}).get("health_rate")
                    hr_str = f" HR={float(hr):.2f}" if hr is not None and float(hr) < 100 else ""
                    label = f"{proto} {dname}{hr_str}".strip()
                    defi_rows: list[tuple[str, str, str]] = []
                    for t in d.get("supply", []):
                        tv = float(t.get("value", 0))
                        if tv >= min_value:
                            sym = t.get("symbol", "?")
                            amt = float(t.get("amount", t.get("quantity", 0)))
                            defi_rows.append((f"+{sym}", _fmt_qty(amt), _fmt_val(tv)))
                    for t in d.get("borrow", []):
                        tv = float(t.get("value", 0))
                        if tv >= min_value:
                            sym = t.get("symbol", "?")
                            amt = float(t.get("amount", t.get("quantity", 0)))
                            defi_rows.append((f"-{sym}", _fmt_qty(amt), f"-{_fmt_val(tv)}"))
                    if defi_rows:
                        print(f"    [{label}]")
                        dw0 = max(len(r[0]) for r in defi_rows)
                        dw1 = max(len(r[1]) for r in defi_rows)
                        dw2 = max(len(r[2]) for r in defi_rows)
                        for tok, q, v in defi_rows:
                            print(f"      {tok:<{dw0}}{sep}{q:>{dw1}}{sep}{v:>{dw2}}")

    elif cmd == "dump":
        output_path = args[0] if args else "tests/fixtures/portfolio_dump.json"
        data = await service.get_portfolio(force_refresh=True)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, default=_json_default, indent=2, ensure_ascii=False))
        print(f"Saved fixture to {out} ({out.stat().st_size:,} bytes)")

    elif cmd == "set-key":
        KNOWN_KEYS = [
            "DEBANK_API_KEY",
            "BINANCE_API_KEY", "BINANCE_API_SECRET",
            "BYBIT_API_KEY", "BYBIT_API_SECRET",
            "BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE",
            "OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE",
            "JUPITER_API_KEY",
            "ALCHEMY_API_KEY", "HELIUS_API_KEY",
        ]
        if not args:
            print(f"Usage: set-key <key_name> [value]")
            print(f"Keys: {', '.join(KNOWN_KEYS)}")
            sys.exit(1)
        key_name = args[0].upper()
        if key_name not in KNOWN_KEYS:
            print(f"Unknown key: {key_name}. Known keys: {', '.join(KNOWN_KEYS)}")
            sys.exit(1)
        import getpass
        value = args[1] if len(args) > 1 else getpass.getpass(f"Enter value for {key_name}: ")
        if set_secret(key_name, value):
            print(f"{key_name} saved to Keychain.")
        else:
            print(f"Failed to save {key_name}.", file=sys.stderr)

    elif cmd == "show-keys":
        KNOWN_KEYS = [
            "DEBANK_API_KEY",
            "BINANCE_API_KEY", "BINANCE_API_SECRET",
            "BYBIT_API_KEY", "BYBIT_API_SECRET",
            "BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE",
            "OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE",
            "JUPITER_API_KEY",
            "ALCHEMY_API_KEY", "HELIUS_API_KEY",
        ]
        for key in KNOWN_KEYS:
            val = get_secret(key)
            if val:
                masked = val[:4] + "..." + val[-4:]
                print(f"  {key}: {masked}")
            else:
                print(f"  {key}: (not set)")

    elif cmd == "add-address":
        if not args:
            print("Usage: add-address <evm|solana> <address>")
            sys.exit(1)
        chain = args[0].lower()
        addr = args[1] if len(args) > 1 else ""
        if chain not in ("evm", "solana") or not addr:
            print("Usage: add-address <evm|solana> <address>")
            sys.exit(1)
        _update_config_addresses(chain, addr, add=True)

    elif cmd == "remove-address":
        if not args:
            print("Usage: remove-address <evm|solana> <address>")
            sys.exit(1)
        chain = args[0].lower()
        addr = args[1] if len(args) > 1 else ""
        if chain not in ("evm", "solana") or not addr:
            print("Usage: remove-address <evm|solana> <address>")
            sys.exit(1)
        _update_config_addresses(chain, addr, add=False)

    elif cmd == "list-addresses":
        print(f"EVM addresses ({len(config.evm_addresses)}):")
        for a in config.evm_addresses:
            print(f"  {a}")
        print(f"\nSolana addresses ({len(config.solana_addresses)}):")
        for a in config.solana_addresses:
            print(f"  {a}")

    elif cmd == "add-manual":
        if len(args) < 3:
            print("Usage: add-manual <project> <coin> <quantity> [--price <usd>] [--notes \"text\"] [--expires <YYYY-MM-DD>]")
            sys.exit(1)
        project, coin = args[0], args[1]
        quantity = float(args[2])
        price_usd = None
        notes = None
        expires_at = None
        i = 3
        while i < len(args):
            if args[i] == "--price" and i + 1 < len(args):
                price_usd = float(args[i + 1])
                i += 2
            elif args[i] == "--notes" and i + 1 < len(args):
                notes = args[i + 1]
                i += 2
            elif args[i] == "--expires" and i + 1 < len(args):
                expires_at = args[i + 1]
                i += 2
            else:
                i += 1
        entry_id = await storage.add_manual_entry(project, coin, quantity, price_usd=price_usd, notes=notes, expires_at=expires_at)
        print(f"Added manual entry #{entry_id}: {project} {coin} {quantity}" + (f" @ ${price_usd}" if price_usd else ""))

    elif cmd == "list-manual":
        show_all = "--all" in args
        entries = await storage.get_manual_entries(active_only=not show_all)
        if not entries:
            print("No manual entries found.")
        else:
            # Table header
            hdr = f"{'ID':>4}  {'Project':<15} {'Coin':<8} {'Qty':>12} {'Price':>10} {'Value':>10} {'Expires':>12} {'Notes'}"
            print(hdr)
            print("-" * len(hdr))
            for e in entries:
                exp = e["expires_at"] or ""
                price = f"${e['price_usd']:,.2f}" if e["price_usd"] is not None else "-"
                value = f"${e['value_usd']:,.0f}" if e["value_usd"] is not None else "-"
                active = "" if e["is_active"] else " [inactive]"
                print(f"{e['id']:>4}  {e['project']:<15} {e['coin']:<8} {e['quantity']:>12,.2f} {price:>10} {value:>10} {exp:>12} {e['notes'] or ''}{active}")

    elif cmd == "remove-manual":
        if not args:
            print("Usage: remove-manual <id>")
            sys.exit(1)
        entry_id = int(args[0])
        await storage.remove_manual_entry(entry_id)
        print(f"Removed manual entry #{entry_id}")

    elif cmd == "check-expiry":
        # Legacy standalone command — now folded into check-health
        print("Note: expiry checks are now included in 'check-health'. Running check-health...")
        # Re-run as check-health
        await _run("check-health", args)

    elif cmd == "report-send":
        """Generate daily report and send to Discord via webhook."""
        from .utils.discord_webhook import send_webhook
        data = await service.get_portfolio()
        data["timestamp"] = float(data.get("timestamp", 0))
        await storage.save_snapshot(data)
        manual = await storage.get_manual_entries(active_only=True)
        if manual:
            data["manual_entries"] = manual
        now = time.time()
        prev = await storage.get_nearest(now - 86400, exclude_after=now - 3600)
        jlp_w = await get_jlp_weights()
        text = format_report(data, prev_data=prev, report_type="daily", jlp_weights=jlp_w)
        webhook_url = config.discord_webhook_url
        if not webhook_url:
            print("Error: discord_webhook_url not set in config.yaml", file=sys.stderr)
            sys.exit(1)
        ok = await send_webhook(webhook_url, text)
        print("Sent daily report to Discord" if ok else "Failed to send report")

    elif cmd == "weekly-send":
        """Generate weekly report and send to Discord via webhook."""
        from .utils.discord_webhook import send_webhook
        data = await service.get_portfolio()
        data["timestamp"] = float(data.get("timestamp", 0))
        await storage.save_snapshot(data)
        manual = await storage.get_manual_entries(active_only=True)
        if manual:
            data["manual_entries"] = manual
        now = time.time()
        prev = await storage.get_nearest(now - 7 * 86400, exclude_after=now - 3600)
        jlp_w = await get_jlp_weights()
        text = format_report(data, prev_data=prev, report_type="weekly", jlp_weights=jlp_w)
        webhook_url = config.discord_webhook_url
        if not webhook_url:
            print("Error: discord_webhook_url not set in config.yaml", file=sys.stderr)
            sys.exit(1)
        ok = await send_webhook(webhook_url, text)
        print("Sent weekly report to Discord" if ok else "Failed to send report")

    elif cmd == "alert-send":
        """Run health check and send alerts to Discord via webhook."""
        from .utils.discord_webhook import send_webhook
        threshold = float(args[0]) if args else 1.5
        min_val = 10.0
        data = await service.get_health_data()
        alerts = []
        for pos in data.get("defi_positions", []):
            h = (pos.get("detail") or {}).get("health_rate")
            if h is not None and float(h) < threshold:
                supply_val = sum(float(t.get("value", 0)) for t in pos.get("supply", []))
                borrow_val = sum(float(t.get("value", 0)) for t in pos.get("borrow", []))
                if supply_val < min_val and borrow_val < min_val:
                    continue
                alerts.append({
                    "protocol": pos.get("protocol"),
                    "chain": pos.get("chain"),
                    "health_rate": float(h),
                    "address": pos.get("address", "")[:6] + "..." + pos.get("address", "")[-4:] if pos.get("address") else "",
                    "supply_usd": supply_val,
                    "borrow_usd": borrow_val,
                })
        for ex_name, ex_data in data.get("exchange_health", {}).items():
            ltv = ex_data.get("ltv", 0)
            if ltv > 0.7:
                alerts.append({
                    "protocol": f"{ex_name} crypto loan",
                    "chain": "cex",
                    "health_rate": round(1 / ltv, 2) if ltv > 0 else 999,
                    "address": ex_name,
                    "supply_usd": ex_data.get("collateral_usd", 0),
                    "borrow_usd": ex_data.get("debt_usd", 0),
                })
        # Check expiring manual entries
        expiring = await storage.get_expiring_entries()
        for e in expiring:
            alerts.append({
                "protocol": "manual_entry",
                "chain": "manual",
                "health_rate": 0,
                "address": "",
                "supply_usd": float(e.get("value_usd", 0) or 0),
                "borrow_usd": 0,
                "message": f"\u23f0 \u5230\u671f\u63d0\u9192: [{e['project']}] {e['coin']} x {e['quantity']}"
                           + (f" \u2014 {e['notes']}" if e.get('notes') else ""),
            })
            await storage.mark_reminded(e["id"])

        if not alerts:
            print("No alerts, nothing to send")
        else:
            lines = [f"\u26a0\ufe0f **\u5065\u5eb7\u544a\u8b66** ({len(alerts)} \u6761)"]
            for a in alerts:
                if a.get("message"):
                    lines.append(a["message"])
                else:
                    lines.append(
                        f"- **{a['protocol']}** ({a['chain']}) {a['address']} "
                        f"HR={a['health_rate']:.2f} "
                        f"(\u62b5\u62bc ${a['supply_usd']:,.0f} / \u501f\u6b3e ${a['borrow_usd']:,.0f})"
                    )
            text = "\n".join(lines)
            webhook_url = config.discord_webhook_url
            if not webhook_url:
                print("Error: discord_webhook_url not set in config.yaml", file=sys.stderr)
                sys.exit(1)
            ok = await send_webhook(webhook_url, text)
            print(f"Sent {len(alerts)} alerts to Discord" if ok else "Failed to send alerts")

    else:
        print("Usage: python -m portfolio_tracker <command>")
        print("Commands: report, portfolio, assets [--all], defi, exchange,")
        print("          wallets [min_value], history [days], snapshot, dump [output],")
        print("          weekly-report, preview [current|daily|weekly],")
        print("          check-health [threshold], set-key <name>, show-keys,")
        print("          add-address <evm|solana> <addr>, remove-address <evm|solana> <addr>,")
        print("          breakdown [min_value], list-addresses,")
        print("          add-manual <project> <coin> <qty> [--price <usd>] [--notes \"text\"] [--expires <YYYY-MM-DD>],")
        print("          list-manual [--all], remove-manual <id>,")
        print("          report-send, weekly-send, alert-send [threshold]")
        sys.exit(1)


def main():
    args = sys.argv[1:]
    if not args:
        args = ["portfolio"]
    cmd = args[0]
    asyncio.run(_run(cmd, args[1:]))


if __name__ == "__main__":
    main()

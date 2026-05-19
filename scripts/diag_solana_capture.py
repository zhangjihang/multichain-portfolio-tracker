"""One-off diagnostic: capture jup.ag body_text for one wallet and compare
TOC-declared protocol totals vs what _parse_page extracts.

Confirms H1 (parser skips present sections) vs H2 (sections absent from
captured text because the cold browser hadn't rendered them at sleep(5)).

Run: .venv/bin/python scripts/diag_solana_capture.py
"""

import asyncio
import sys
from decimal import Decimal

from portfolio_tracker.adapters.solana_scraper import SolanaScraper

if len(sys.argv) < 2:
    sys.exit("usage: diag_solana_capture.py <solana_address>")
ADDR = sys.argv[1]
DUMP = f"/tmp/jup_{ADDR[:8]}_body.txt"


async def main():
    s = SolanaScraper()
    await s._ensure_browser()
    page = await s._context.new_page()
    url = f"{s.BASE_URL}/{ADDR}"
    print(f"goto {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_selector("text=Net Worth", timeout=45000)

    body = await page.inner_text("body")
    if "Proof of humanity required" in body:
        print("Turnstile present — waiting up to 60s for manual solve...")
        try:
            await page.wait_for_function(
                "() => !document.body.innerText.includes('Proof of humanity required')",
                timeout=60000,
            )
        except Exception:
            print("Turnstile NOT resolved")

    # Capture at the SAME point the scraper does (after fixed 5s)
    await asyncio.sleep(5)
    body_5s = await page.inner_text("body")
    open(DUMP + ".5s", "w").write(body_5s)

    # Then capture again much later to see if late sections appear
    await asyncio.sleep(25)
    body_30s = await page.inner_text("body")
    open(DUMP + ".30s", "w").write(body_30s)

    await page.close()
    await s.close()

    for label, text in (("5s", body_5s), ("30s", body_30s)):
        r = s._parse_page(text)
        nlines = len(text.splitlines())
        defi_sum = Decimal("0")
        for p in r.get("defi_positions", []):
            for t in p.get("supply", []):
                defi_sum += t.get("value", Decimal("0"))
        kam = text.count("Kamino")
        print(
            f"[{label}] body_lines={nlines} net_worth={r.get('net_worth')} "
            f"defi_positions={len(r.get('defi_positions', []))} defi_supply_sum=${defi_sum:,.0f} "
            f"holdings={len(r.get('holdings', []))} 'Kamino'x{kam} "
            f"PYUSD_in_text={'PYUSD' in text} CARDS_in_text={'CARDS' in text}"
        )
    print(f"dumps: {DUMP}.5s , {DUMP}.30s")


if __name__ == "__main__":
    asyncio.run(main())

"""Faithful failure-mode diagnostic for the Solana scraper.

Uses the project's own SolanaScraper browser path (same _ensure_browser:
CDP:18800 -> cold persistent Chrome fallback) so it reproduces the cron
scraper's conditions exactly. Instruments each phase with timing, Turnstile
detection, periodic snapshots of Net Worth / DeFi-section presence, and
screenshots, so we can tell WHY a heavy wallet fails:

  - Turnstile blocks            -> body keeps "Proof of humanity required"
  - load/render too slow        -> Net Worth / DeFi appear only after >5s
  - never renders / timeout     -> selector/text never appears

Run: .venv/bin/python scripts/diag_solana_timeline.py [ADDR]
"""

import asyncio
import sys
import time

from portfolio_tracker.adapters.solana_scraper import SolanaScraper

if len(sys.argv) < 2:
    sys.exit("usage: diag_solana_timeline.py <solana_address>")
ADDR = sys.argv[1]
OUT = f"/tmp/diag_{ADDR[:8]}"


def _now(t0):
    return f"{time.monotonic() - t0:6.1f}s"


async def main():
    s = SolanaScraper()
    t0 = time.monotonic()
    print(f"[{_now(t0)}] ensuring browser ...")
    await s._ensure_browser()
    print(f"[{_now(t0)}] browser ready  is_cdp={getattr(s, '_is_cdp', '?')}")

    page = await s._context.new_page()
    url = f"{s.BASE_URL}/{ADDR}"
    print(f"[{_now(t0)}] goto {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    print(f"[{_now(t0)}] domcontentloaded")

    try:
        await page.wait_for_selector("text=Net Worth", timeout=45000)
        print(f"[{_now(t0)}] 'Net Worth' selector appeared")
    except Exception as e:
        print(f"[{_now(t0)}] 'Net Worth' NEVER appeared: {e!r}")

    # Poll every 3s for ~45s and record what's on the page.
    for k in range(16):
        body = await page.inner_text("body")
        turnstile = "Proof of humanity required" in body
        parsed = s._parse_page(body)
        nw = parsed.get("net_worth")
        ndefi = len(parsed.get("defi_positions", []))
        nhold = len(parsed.get("holdings", []))
        has_kamino = "Kamino" in body
        no_asset = "No asset detected" in body
        print(
            f"[{_now(t0)}] turnstile={turnstile} no_asset={no_asset} "
            f"bodylen={len(body)} netWorth={nw} holdings={nhold} "
            f"defi={ndefi} KaminoInText={has_kamino}"
        )
        if k in (1, 4, 9, 15):
            try:
                await page.screenshot(path=f"{OUT}_t{int(time.monotonic()-t0)}.png")
            except Exception:
                pass
        await asyncio.sleep(3)

    open(f"{OUT}_final_body.txt", "w").write(await page.inner_text("body"))
    print(f"[{_now(t0)}] done. screenshots/body at {OUT}_*")
    await page.close()
    await s.close()


if __name__ == "__main__":
    asyncio.run(main())

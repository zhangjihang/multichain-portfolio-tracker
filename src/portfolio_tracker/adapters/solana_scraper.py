"""Solana DeFi portfolio scraper via jup.ag/portfolio (SonarWatch engine).

Uses Playwright to load the Jupiter Portfolio page and extract structured
DeFi position data including holdings, lending, staking, farming, and rewards.
"""

import asyncio
import logging
import re
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

_DOLLAR_RE = re.compile(r'[<>]?\$[\d,]+\.?\d*')


def _parse_dollar(s: str) -> Decimal:
    """Parse '$1,234.56' or '<$0.01' to Decimal."""
    if not s:
        return Decimal("0")
    s = s.strip()
    # jup.ag wraps some values in parentheses, e.g. "($1,234.56)"
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    if s.startswith("<"):
        return Decimal("0.005")
    s = s.replace("$", "").replace(",", "")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_quantity(s: str) -> Decimal:
    """Parse quantity, handling Chinese 万 (10000)."""
    if not s:
        return Decimal("0")
    s = s.strip().replace(",", "")
    if "万" in s:
        s = s.replace("万", "")
        try:
            return Decimal(s) * Decimal("10000")
        except (InvalidOperation, ValueError):
            return Decimal("0")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parsed_ready(prev: dict | None, curr: dict) -> bool:
    """Has the jup.ag page settled enough to trust the parse?

    Replaces the fixed sleep(5): heavy wallets on a cold browser only
    populate Net Worth / DeFi sections at ~8-11s. Ready iff the current
    parse has a non-zero net worth AND is stable vs the previous poll
    (same net worth and same DeFi-position count), i.e. data + all
    protocol sections have finished loading.
    """
    if curr.get("net_worth", Decimal("0")) <= Decimal("0"):
        return False
    if prev is None:
        return False
    if prev.get("net_worth", Decimal("0")) != curr.get("net_worth", Decimal("0")):
        return False
    return len(prev.get("defi_positions", [])) == len(curr.get("defi_positions", []))


class SolanaScraper:
    """Scrape Solana DeFi positions from jup.ag/portfolio."""

    BASE_URL = "https://jup.ag/portfolio"

    def __init__(self):
        self._browser = None
        self._context = None
        self._is_cdp = False

    async def _ensure_browser(self):
        """Launch or connect to a browser for scraping.

        Strategy:
        1. Try connecting to an existing browser via CDP on port 18800.
        2. Launch system Chrome with a persistent user-data-dir.
           The persistent profile keeps Turnstile cookies so the challenge
           only needs to be solved once manually.
        """
        if self._browser is not None:
            return

        from pathlib import Path
        from playwright.async_api import async_playwright
        import httpx

        self._pw = await async_playwright().start()

        # 1. Try existing CDP browser (port 18800)
        for cdp_attempt in range(1, 3):
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get("http://127.0.0.1:18800/json/version")
                    ws_url = resp.json().get("webSocketDebuggerUrl")
                    if not ws_url:
                        raise RuntimeError("No webSocketDebuggerUrl in CDP response")
                    self._browser = await self._pw.chromium.connect_over_cdp(ws_url)
                    contexts = self._browser.contexts
                    self._context = contexts[0] if contexts else await self._browser.new_context(
                        viewport={"width": 1280, "height": 900},
                    )
                    self._is_cdp = True
                    logger.info("Connected to existing browser via CDP")
                    return
            except Exception:
                if cdp_attempt < 2:
                    logger.info("CDP connection attempt %d failed, retrying...", cdp_attempt)
                    await asyncio.sleep(1)
                else:
                    logger.info("CDP connection failed, launching persistent Chrome")

        # 2. Launch Chrome with persistent user-data-dir (keeps Turnstile cookies)
        user_data = Path(__file__).resolve().parent.parent.parent.parent / "data" / "chrome-profile"
        user_data.mkdir(parents=True, exist_ok=True)

        for channel in ("chrome", "chromium", None):
            try:
                self._context = await self._pw.chromium.launch_persistent_context(
                    str(user_data),
                    headless=False,
                    channel=channel,
                    viewport={"width": 1280, "height": 900},
                    args=[
                        "--disable-blink-features=AutomationControlled",
                    ],
                    ignore_default_args=["--enable-automation"],
                )
                # launch_persistent_context returns a BrowserContext, not a Browser
                self._browser = self._context  # for close() compatibility
                self._is_cdp = False
                label = {"chrome": "system Chrome", "chromium": "Playwright Chromium"}.get(channel, "bundled Chromium")
                logger.info("Launched %s with persistent profile at %s", label, user_data)
                return
            except Exception as e:
                if channel is not None:
                    logger.info("Channel '%s' not available: %s", channel, e)
                    continue
                raise RuntimeError(
                    f"Failed to launch browser. Install Chrome or run: "
                    f"playwright install chromium. Error: {e}"
                ) from e

    async def close(self):
        """Clean up."""
        try:
            if self._browser:
                await self._browser.close()
            if hasattr(self, '_pw') and self._pw:
                await self._pw.stop()
        except Exception:
            logger.exception("Error closing Solana scraper")
        finally:
            self._browser = None
            self._context = None

    async def _scrape_once(self, address: str) -> dict | None:
        """Single scrape attempt. Returns parsed data or None on failure."""
        await self._ensure_browser()
        page = await self._context.new_page()
        try:
            url = f"{self.BASE_URL}/{address}"
            logger.info("Scraping Solana portfolio: %s", address[:10])
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Wait for page content to load
            try:
                await page.wait_for_selector("text=Net Worth", timeout=45000)
            except Exception:
                logger.warning("Timeout waiting for portfolio page for %s", address[:10])
                return None

            # Handle Turnstile
            body_text = await page.inner_text("body")
            if "Proof of humanity required" in body_text:
                logger.info("Turnstile detected for %s, waiting...", address[:10])
                try:
                    await page.wait_for_function(
                        "() => !document.body.innerText.includes('Proof of humanity required')",
                        timeout=30000,
                    )
                except Exception:
                    logger.warning("Turnstile did not resolve for %s", address[:10])
                    return None

            # Poll until the page data has settled (replaces the fixed
            # sleep(5)). Heavy wallets on a cold browser only populate Net
            # Worth / DeFi sections at ~8-11s; a blind 5s wait captured an
            # empty $0 page and the wallet was silently dropped.
            prev = None
            saw_no_asset = False
            for _ in range(22):  # ~44s ceiling at 2s/poll
                await asyncio.sleep(2)
                body_text = await page.inner_text("body")

                if "Proof of humanity required" in body_text:
                    logger.info("Turnstile (late) for %s, waiting...", address[:10])
                    try:
                        await page.wait_for_function(
                            "() => !document.body.innerText.includes('Proof of humanity required')",
                            timeout=30000,
                        )
                    except Exception:
                        logger.warning("Turnstile did not resolve for %s", address[:10])
                        return None
                    continue

                if "No asset detected" in body_text:
                    # Require two consecutive confirmations: a pre-render
                    # flash can momentarily show "No asset detected" before
                    # data loads — accepting it on first sight returns a
                    # false $0 and bypasses the hard-recovery retry.
                    if saw_no_asset:
                        return {
                            "net_worth": Decimal("0"), "holdings": [],
                            "defi_positions": [], "total_assets": Decimal("0"),
                            "total_debts": Decimal("0"),
                        }
                    saw_no_asset = True
                    continue
                saw_no_asset = False

                curr = self._parse_page(body_text)
                if _parsed_ready(prev, curr):
                    return curr
                prev = curr

            logger.warning(
                "Solana page for %s did not settle in time (last net worth %s) — retrying",
                address[:10], (prev or {}).get("net_worth", Decimal("0")),
            )
            return None
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # Backoff (seconds) before each retry. len + 1 == max attempts.
    _RETRY_BACKOFF = (5, 10, 20, 30)

    async def scrape_portfolio(self, address: str) -> dict:
        """Scrape a Solana address, recovering hard until data is obtained.

        The data does load (proven ~10s); a failed attempt means a
        transient/wedged browser state, not missing data. So between
        attempts we tear the browser down completely and rebuild it fresh
        (clears cold-start/wedged state), with escalating backoff, instead
        of silently accepting $0. Only a sustained jup.ag outage across the
        whole window can still yield empty.
        """
        empty = {
            "net_worth": Decimal("0"), "holdings": [],
            "defi_positions": [], "total_assets": Decimal("0"), "total_debts": Decimal("0"),
        }

        max_attempts = len(self._RETRY_BACKOFF) + 1
        for attempt in range(1, max_attempts + 1):
            try:
                result = await self._scrape_once(address)
                if result is not None:
                    nw = float(result.get("net_worth", 0))
                    logger.info("Scraped %s: $%.0f (attempt %d)", address[:10], nw, attempt)
                    return result
                reason = "no data / page never settled"
            except Exception:
                logger.warning("Scrape attempt %d/%d errored for %s",
                                attempt, max_attempts, address[:10], exc_info=True)
                reason = "exception"

            if attempt < max_attempts:
                backoff = self._RETRY_BACKOFF[attempt - 1]
                logger.warning(
                    "Scrape attempt %d/%d for %s failed (%s) — rebuilding "
                    "browser, retrying in %ds",
                    attempt, max_attempts, address[:10], reason, backoff,
                )
                # Hard recovery: drop the (possibly wedged/cold-broken)
                # browser so the next attempt starts from a clean one.
                try:
                    await self.close()
                except Exception:
                    logger.debug("Browser teardown during retry failed", exc_info=True)
                await asyncio.sleep(backoff)
            else:
                logger.error(
                    "Failed to scrape %s after %d attempts (%s)",
                    address[:10], max_attempts, reason,
                )

        return empty

    def _parse_page(self, text: str) -> dict:
        """Parse the page text into structured portfolio data.

        Page structure (illustrative line indices; values are placeholders):
        ---
        45: Positions / 46: Activity
        47: Collapse
        48: Wxxx...xxxx - $123,456.78
        51: Holdings / 52: $99,999.99
        53: Kamino / 54: $88,888.88
        55: Jupiter DAO / 56: $7,777.77
        ...
        59: Holdings (repeat = start of holdings detail)
        60: $99,999.99 / 61: Wallet / ...
        63: Asset\tBalance\tPrice/24hΔ\tValue  (table header)
        64-122: token rows (symbol, qty, price, pct_change, value)
        123: Kamino (protocol detail start)
        125: Lending / 126: Main Market / 127: Health / 128: 51%
        ...
        179: Jupiter DAO (next protocol)
        ...
        """
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        result = {
            "net_worth": Decimal("0"), "holdings": [],
            "defi_positions": [], "total_assets": Decimal("0"), "total_debts": Decimal("0"),
        }

        # Find Net Worth
        for i, line in enumerate(lines):
            if line == "Net Worth" and i + 1 < len(lines):
                result["net_worth"] = _parse_dollar(lines[i + 1])
                break

        # Find the summary TOC: after "Collapse" line, before the second "Holdings"
        # The TOC lists: Holdings $X, Kamino $Y, Jupiter DAO $Z, ...
        # Then the detail sections start at the second "Holdings"
        toc_protocols = []  # [(name, value), ...]
        collapse_idx = None
        detail_start_idx = None

        for i, line in enumerate(lines):
            if line == "Collapse":
                collapse_idx = i
                break

        if collapse_idx is not None:
            # Skip the address line after Collapse, then Bug/Feedback
            i = collapse_idx + 1
            while i < len(lines) and (lines[i] in ("Bug", "Feedback") or " - $" in lines[i]):
                i += 1

            # Read TOC entries: name, $value pairs
            # Stop when we see a name we've already seen (second "Holdings" = detail start)
            seen_names = set()
            while i + 1 < len(lines):
                name = lines[i]
                val_line = lines[i + 1]
                if name in seen_names:
                    break  # Repeated name = detail section starts
                if _DOLLAR_RE.match(val_line):
                    toc_protocols.append((name, _parse_dollar(val_line)))
                    seen_names.add(name)
                    i += 2
                else:
                    break

            # The detail sections start here (at the repeated "Holdings")
            detail_start_idx = i

        if detail_start_idx is None:
            return result

        # Now parse the detail sections
        # First section is always "Holdings" (wallet tokens)
        # Subsequent sections are protocols (Kamino, Jupiter DAO, etc.)
        protocol_names = [p[0] for p in toc_protocols if p[0] != "Holdings"]
        
        # Find where each protocol's detail section starts
        # Protocol detail sections start with the protocol name repeated
        section_boundaries = []  # [(name, start_idx), ...]

        i = detail_start_idx
        while i < len(lines):
            line = lines[i]
            if line == "Holdings" and not section_boundaries:
                section_boundaries.append(("Holdings", i))
            elif line in protocol_names:
                section_boundaries.append((line, i))
            elif line == "Back to top":
                break
            i += 1

        # Parse each section
        for idx, (name, start) in enumerate(section_boundaries):
            end = section_boundaries[idx + 1][1] if idx + 1 < len(section_boundaries) else len(lines)
            section_lines = lines[start:end]

            if name == "Holdings":
                result["holdings"] = self._parse_holdings_section(section_lines)
            else:
                positions = self._parse_protocol_detail(name, section_lines)
                result["defi_positions"].extend(positions)

        # Calculate totals
        result["total_assets"] = sum(h["value"] for h in result["holdings"])
        for pos in result["defi_positions"]:
            for s in pos.get("supply", []):
                result["total_assets"] += s.get("value", Decimal("0"))
            for b in pos.get("borrow", []):
                result["total_debts"] += b.get("value", Decimal("0"))
            # Only add the aggregate position value when no token-level
            # supply rows were parsed; otherwise supply already counts it
            # (adding both double-counted total_assets).
            if (pos["type"] in ("staking", "farming", "deposit", "airdrop", "rewards")
                    and not pos.get("supply")):
                result["total_assets"] += pos.get("value", Decimal("0"))

        return result

    def _parse_holdings_section(self, lines: list[str]) -> list[dict]:
        """Parse the Holdings/Wallet token table.

        Structure:
        Holdings / $99,999.99 / Wallet / $99,999.99
        Asset\tBalance\tPrice/24hΔ\tValue  (header)
        JLP / 13.24% / 12,345 / $1.2345 / -0.64% / $77,777.77
        CARDS / 23,456 / $0.0100 / +11.82% / $1,111.11
        ...
        """
        holdings = []

        # Find the table header. jup.ag added a "PnL (all time)" column, which
        # pushed "Value" to a wrapped line — so don't require Value on the header line.
        header_idx = None
        for i, line in enumerate(lines):
            if "Asset" in line and "Balance" in line:
                header_idx = i
                break

        if header_idx is None:
            return holdings

        # Token symbol detector. Excludes em-dash (—) used as a placeholder for
        # missing PnL, and "<+$X"/"<-$X" tiny-value markers.
        def _is_token_symbol(s: str) -> bool:
            return (
                len(s) <= 15
                and s != "—"
                and not s.startswith("$")
                and not s.startswith("<")
                and not s.startswith("+")
                and not s.startswith("-")
                and not re.match(r'^[\d,.]+$', s)
                and not re.match(r'^[\d.]+%$', s)
                and s not in ("Wallet", "Asset", "Balance", "Price/24hΔ", "Value", "Collapse")
            )

        # Parse token rows after header
        i = header_idx + 1
        while i < len(lines):
            line = lines[i]

            # Stop at next section indicator
            if line in ("Back to top", "Bug", "Feedback") or (line != "Holdings" and i > header_idx + 2 and "\t" in line and "Asset" in line):
                break

            if _is_token_symbol(line):
                symbol = line
                # Collect subsequent lines until next symbol
                j = i + 1
                values = []
                while j < len(lines) and j < i + 8:
                    vl = lines[j]
                    if _is_token_symbol(vl):
                        break
                    values.append(vl)
                    j += 1

                # Parse: optional pct, quantity, price, optional pct_change, value
                quantity = Decimal("0")
                price = Decimal("0")
                value = Decimal("0")
                dollar_vals = []
                qty_candidates = []

                for v in values:
                    if _DOLLAR_RE.match(v):
                        dollar_vals.append(_parse_dollar(v))
                    elif re.match(r'^[+-]?[\d.]+%$', v):
                        continue  # skip percentage changes
                    elif re.match(r'^[\d,.]+$', v):
                        qty_candidates.append(_parse_quantity(v))
                    # Handle tab-separated values like "4.03% APY\t$66,666.66"
                    elif "\t" in v:
                        for part in v.split("\t"):
                            part = part.strip()
                            if _DOLLAR_RE.match(part):
                                dollar_vals.append(_parse_dollar(part))

                if qty_candidates:
                    quantity = qty_candidates[0]
                if len(dollar_vals) >= 2:
                    price = dollar_vals[0]
                    value = dollar_vals[-1]
                elif len(dollar_vals) == 1:
                    value = dollar_vals[0]

                if value > Decimal("0.01"):
                    holdings.append({
                        "symbol": symbol,
                        "quantity": quantity,
                        "price": price,
                        "value": value,
                    })
                i = j
                continue

            i += 1

        return holdings

    def _parse_protocol_detail(self, protocol: str, lines: list[str]) -> list[dict]:
        """Parse a protocol detail section (Kamino, Jupiter DAO, etc.).

        Kamino example:
        Kamino / $88,888.88 / Lending / Main Market / Health / 51% / $88,800.00
        Supplied / $66,666.66 / Token\tBalance\t... / wSOL / 1,000 wSOL / $10.0 / ...
        Borrowed / $50,000.00 / ... / CASH / 50,000 CASH / ...
        Farming / $321.00 / ...
        Rewards / ... / CASH / Claimable / 100.00 CASH / $100.00 / ...

        Jupiter DAO example:
        Jupiter DAO / $5,000.00 / Staked / $4,500.00 / ... / JUP / Locked / 10,000 / ...
        Airdrop / ASR Oct-Dec 2025 / $200.00 / ... / JUP / Claim / 1,000 / ...
        """
        positions = []
        
        # Split into sub-sections: Lending, Farming, Staked, Airdrop, Deposit, Rewards, Swap Tips
        sub_section_names = {"Lending", "Farming", "Staked", "Airdrop", "Deposit", "Rewards", "Swap Tips"}
        sub_sections = []  # [(type, start, end)]
        
        for i, line in enumerate(lines):
            if line in sub_section_names:
                sub_sections.append((line, i))

        if not sub_sections:
            return positions

        for idx, (sub_type, start) in enumerate(sub_sections):
            end = sub_sections[idx + 1][1] if idx + 1 < len(sub_sections) else len(lines)
            sub_lines = lines[start:end]

            if sub_type == "Lending":
                pos = self._parse_lending(protocol, sub_lines)
                if pos:
                    positions.append(pos)
            elif sub_type == "Staked":
                pos = self._parse_staking(protocol, sub_lines)
                if pos:
                    positions.append(pos)
            elif sub_type == "Airdrop":
                pos = self._parse_airdrop(protocol, sub_lines)
                if pos:
                    positions.append(pos)
            elif sub_type == "Deposit":
                pos = self._parse_deposit(protocol, sub_lines)
                if pos:
                    positions.append(pos)
            elif sub_type in ("Farming", "Rewards", "Swap Tips"):
                pos = self._parse_farming_rewards(protocol, sub_type, sub_lines)
                if pos:
                    positions.append(pos)

        return positions

    def _parse_lending(self, protocol: str, lines: list[str]) -> dict | None:
        """Parse Lending sub-section with Supplied/Borrowed/Health."""
        pos = {
            "protocol": protocol, "type": "lending", "name": "",
            "health_rate": None, "supply": [], "borrow": [], "rewards": [],
            "value": Decimal("0"),
        }

        # Find name and health
        for i, line in enumerate(lines):
            if "Market" in line:
                pos["name"] = line
            if line == "Health" and i + 1 < len(lines):
                pct = re.match(r'(\d+)%', lines[i + 1])
                if pct:
                    pos["health_rate"] = float(pct.group(1)) / 100.0

        # Parse Supplied and Borrowed
        mode = None  # 'supplied' | 'borrowed'
        for i, line in enumerate(lines):
            if line == "Supplied":
                mode = "supplied"
                continue
            elif line == "Borrowed":
                mode = "borrowed"
                continue
            elif line in ("Farming", "Rewards", "Swap Tips"):
                break

            if mode and re.match(r'^[\d,.]+ \w+$', line):
                # "1,000 wSOL" or "50,000 CASH"
                parts = line.split(None, 1)
                if len(parts) == 2:
                    quantity = _parse_quantity(parts[0])
                    symbol = parts[1]
                    # Find price and value in nearby lines
                    price = Decimal("0")
                    value = Decimal("0")
                    apy = ""
                    for j in range(i + 1, min(i + 5, len(lines))):
                        nl = lines[j]
                        if "\t" in nl:
                            for part in nl.split("\t"):
                                part = part.strip()
                                if _DOLLAR_RE.match(part):
                                    value = _parse_dollar(part)
                                elif "APY" in part:
                                    apy = part
                        elif _DOLLAR_RE.match(nl) and price == 0:
                            price = _parse_dollar(nl)
                    
                    entry = {"symbol": symbol, "quantity": quantity, "price": price, "value": value, "apy": apy}
                    if mode == "supplied":
                        pos["supply"].append(entry)
                    else:
                        pos["borrow"].append(entry)

        # Parse rewards within lending section
        in_rewards = False
        for i, line in enumerate(lines):
            if line.startswith("Rewards"):
                in_rewards = True
                continue
            if in_rewards and line == "Claimable" and i + 1 < len(lines):
                # Next line: "100.00 CASH\t\t$100.00" or "100.00 CASH"
                reward_line = lines[i + 1]
                parts = reward_line.split("\t")
                token_part = parts[0].strip()
                value = Decimal("0")
                for p in parts[1:]:
                    p = p.strip()
                    if _DOLLAR_RE.match(p):
                        value = _parse_dollar(p)
                
                token_parts = token_part.split(None, 1)
                if len(token_parts) == 2:
                    qty = _parse_quantity(token_parts[0])
                    sym = token_parts[1]
                    # Look for symbol on previous line
                    pos["rewards"].append({
                        "symbol": sym, "quantity": qty,
                        "value": value, "claimable": True,
                    })

        supply_total = sum(s["value"] for s in pos["supply"])
        borrow_total = sum(b["value"] for b in pos["borrow"])
        pos["value"] = supply_total - borrow_total

        if pos["supply"] or pos["borrow"]:
            return pos
        return None

    def _parse_staking(self, protocol: str, lines: list[str]) -> dict | None:
        """Parse Staked sub-section."""
        pos = {
            "protocol": protocol, "type": "staking", "name": "Staked",
            "health_rate": None, "supply": [], "borrow": [], "rewards": [],
            "value": Decimal("0"),
        }

        # Find the value after "Staked"
        if len(lines) > 1 and _DOLLAR_RE.match(lines[1]):
            pos["value"] = _parse_dollar(lines[1])

        # Find token: symbol / Locked / quantity / price / pct / apy+value
        for i, line in enumerate(lines):
            if line in ("Locked", "Unlocked"):
                # Previous line should be symbol, next line quantity
                symbol = lines[i - 1] if i > 0 else ""
                quantity = Decimal("0")
                price = Decimal("0")
                value = Decimal("0")
                apy = ""
                for j in range(i + 1, min(i + 6, len(lines))):
                    nl = lines[j]
                    if re.match(r'^[\d,]+$', nl):
                        quantity = _parse_quantity(nl)
                    elif _DOLLAR_RE.match(nl) and price == 0:
                        price = _parse_dollar(nl)
                    elif "\t" in nl:
                        for part in nl.split("\t"):
                            part = part.strip()
                            if _DOLLAR_RE.match(part):
                                value = _parse_dollar(part)
                            elif "APY" in part:
                                apy = part

                if symbol and (value > 0 or quantity > 0):
                    pos["supply"].append({
                        "symbol": symbol, "quantity": quantity,
                        "price": price, "value": value, "apy": apy,
                    })

        # Fallback for the table layout (e.g. Kamino Staked) which has no
        # Locked/Unlocked marker: "<hdr w/ Balance & Value>" then per token
        # SYMBOL / <qty> / $price / -x% / \t$value
        if not pos["supply"]:
            _NOISE = {"Name", "Token", "Balance", "Yield", "Value",
                      "Back to top", "Bug", "Feedback", "Claimable"}

            def _is_symbol(s: str, raw: str) -> bool:
                return bool(
                    s and len(s) <= 15 and "\t" not in raw
                    and not s.startswith(("$", "<$", "+", "-"))
                    and not re.match(r'^[\d,.]+', s)
                    and not re.match(r'^[\d.]+%', s)
                    and "APY" not in s and s not in _NOISE
                )

            header = None
            for i, l in enumerate(lines):
                if "\t" in l and "Balance" in l and "Value" in l:
                    header = i
                    break

            if header is not None:
                i = header + 1
                while i < len(lines):
                    raw = lines[i]
                    sym = raw.strip()
                    if not _is_symbol(sym, raw):
                        i += 1
                        continue
                    quantity = Decimal("0")
                    dollars: list[Decimal] = []  # column order: price, then value
                    apy = ""
                    for j in range(i + 1, min(i + 7, len(lines))):
                        nl = lines[j]
                        ns = nl.strip()
                        if _is_symbol(ns, nl):
                            break  # next token row
                        qm = re.match(r'^([\d,.]+)\s+\w+$', ns)
                        if qm:
                            quantity = _parse_quantity(qm.group(1))
                        elif re.match(r'^[\d,.]+$', ns):
                            quantity = _parse_quantity(ns)
                        parts = nl.split("\t") if "\t" in nl else [ns]
                        for part in parts:
                            part = part.strip()
                            if part.startswith(("$", "($", "<$")):
                                dollars.append(_parse_dollar(part))
                            elif "APY" in part:
                                apy = part
                    # Column order is Price then Value: last $ is the position
                    # value, first is the unit price.
                    value = dollars[-1] if dollars else Decimal("0")
                    price = dollars[0] if len(dollars) > 1 else (
                        value / quantity if quantity > 0 else Decimal("0")
                    )
                    if value > 0 or quantity > 0:
                        pos["supply"].append({
                            "symbol": sym, "quantity": quantity,
                            "price": price, "value": value, "apy": apy,
                        })
                    i += 1

        if pos["supply"] or pos["value"] > 0:
            return pos
        return None

    def _parse_airdrop(self, protocol: str, lines: list[str]) -> dict | None:
        """Parse Airdrop sub-section."""
        pos = {
            "protocol": protocol, "type": "airdrop", "name": "",
            "health_rate": None, "supply": [], "borrow": [], "rewards": [],
            "value": Decimal("0"),
        }

        # Name on next line after "Airdrop"
        if len(lines) > 1:
            pos["name"] = lines[1] if not _DOLLAR_RE.match(lines[1]) else "Airdrop"

        for i, line in enumerate(lines):
            if line == "Claim" and i + 1 < len(lines):
                # quantity line, then price, pct, value
                qty_line = lines[i + 1] if i + 1 < len(lines) else ""
                quantity = _parse_quantity(qty_line)
                # Find symbol (before Claim)
                symbol = lines[i - 1] if i > 0 and len(lines[i - 1]) <= 10 else ""
                value = Decimal("0")
                for j in range(i + 2, min(i + 6, len(lines))):
                    if _DOLLAR_RE.match(lines[j]):
                        value = _parse_dollar(lines[j])

                if symbol and (value > 0 or quantity > 0):
                    pos["rewards"].append({
                        "symbol": symbol, "quantity": quantity,
                        "value": value, "claimable": True,
                    })
                    pos["value"] += value

        if pos["rewards"]:
            return pos
        return None

    def _parse_deposit(self, protocol: str, lines: list[str]) -> dict | None:
        """Parse Deposit sub-section.

        Current jup.ag layout (Kamino Farming/Deposit) lists each underlying
        token as:
            <SYMBOL>
            ...
            <qty> <SYMBOL>      e.g. "250,394 PYUSD"
            ($<value>)          e.g. "($1,234.56)"
            <tab> <APY>
        Older/other layouts only expose an aggregate dollar value; fall back
        to that so the position is still counted.
        """
        pos = {
            "protocol": protocol, "type": "deposit", "name": "Deposit",
            "health_rate": None, "supply": [], "borrow": [], "rewards": [],
            "value": Decimal("0"),
        }
        if len(lines) > 1 and _DOLLAR_RE.match(lines[1]):
            pos["value"] = _parse_dollar(lines[1])

        # Token rows: anchor on the "<qty> <SYMBOL>" balance line, then take
        # the next parenthesised / dollar value as that token's USD value.
        for i, line in enumerate(lines):
            m = re.match(r'^([\d,.]+)\s+([A-Za-z][A-Za-z0-9]{0,11})$', line.strip())
            if not m:
                continue
            quantity = _parse_quantity(m.group(1))
            symbol = m.group(2)
            value = Decimal("0")
            for j in range(i + 1, min(i + 4, len(lines))):
                nl = lines[j].strip()
                if nl.startswith("($") or nl.startswith("$") or nl.startswith("<$"):
                    value = _parse_dollar(nl)
                    break
            if value > Decimal("0.01") or quantity > 0:
                pos["supply"].append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "price": (value / quantity) if quantity > 0 else Decimal("0"),
                    "value": value,
                    "apy": "",
                })

        if pos["supply"] or pos["value"] > 0:
            return pos
        return None

    def _parse_farming_rewards(self, protocol: str, sub_type: str, lines: list[str]) -> dict | None:
        """Parse Farming / Rewards / Swap Tips sub-section.

        Farming page structure example:
            Farming / $44,444.44
            Asset\tBalance\tYield\tValue
            PYUSD
            10,000 PYUSD
            1.35% APY\t$44,444.44
        """
        pos = {
            "protocol": protocol, "type": sub_type.lower().replace(" ", "_"),
            "name": sub_type, "health_rate": None,
            "supply": [], "borrow": [], "rewards": [],
            "value": Decimal("0"),
        }
        if len(lines) > 1 and _DOLLAR_RE.match(lines[1]):
            pos["value"] = _parse_dollar(lines[1])

        # Try to extract token details from Farming/Rewards table rows
        # Look for: symbol line -> "qty SYMBOL" line -> "APY\t$value" line
        header_idx = None
        for i, line in enumerate(lines):
            if "Asset" in line and ("Balance" in line or "Value" in line):
                header_idx = i
                break

        if header_idx is not None:
            i = header_idx + 1
            while i < len(lines):
                line = lines[i]
                # Detect token symbol: short text, not a dollar value, not a number
                if (
                    len(line) <= 15
                    and not line.startswith("$")
                    and not line.startswith("<$")
                    and not line.startswith("+")
                    and not line.startswith("-")
                    and not re.match(r'^[\d,.]+$', line)
                    and not re.match(r'^[\d.]+%', line)
                    and line not in ("Asset", "Balance", "Yield", "Value", "Back to top",
                                     "Bug", "Feedback", "Claimable")
                    and "\t" not in line
                    and not re.match(r'^[\d,.]+ \w+', line)  # skip "10,000 PYUSD" qty lines
                ):
                    symbol = line
                    quantity = Decimal("0")
                    value = Decimal("0")
                    apy = ""

                    # Scan next few lines for qty and value
                    for j in range(i + 1, min(i + 5, len(lines))):
                        nl = lines[j]
                        # "10,000 PYUSD" pattern
                        qty_match = re.match(r'^([\d,.]+)\s+\w+', nl)
                        if qty_match and not _DOLLAR_RE.match(nl):
                            quantity = _parse_quantity(qty_match.group(1))
                        # Tab-separated: "1.35% APY\t$44,444.44"
                        if "\t" in nl:
                            for part in nl.split("\t"):
                                part = part.strip()
                                if _DOLLAR_RE.match(part):
                                    value = _parse_dollar(part)
                                elif "APY" in part:
                                    apy = part
                        elif _DOLLAR_RE.match(nl):
                            value = _parse_dollar(nl)

                    if value > Decimal("0.01") or quantity > Decimal("0"):
                        pos["supply"].append({
                            "symbol": symbol,
                            "quantity": quantity,
                            "price": (value / quantity) if quantity > 0 else Decimal("0"),
                            "value": value,
                            "apy": apy,
                        })

                i += 1

        return pos if pos["value"] > Decimal("0.01") or pos["supply"] else None


async def scrape_solana_portfolios(
    addresses: list[str],
    max_concurrent: int = 1,
) -> dict[str, dict]:
    """Scrape multiple Solana addresses sequentially for stability.

    Uses max_concurrent=1 by default to avoid browser tab contention.
    Each address gets up to 3 retries with exponential backoff.
    """
    scraper = SolanaScraper()
    results = {}
    empty = {
        "net_worth": Decimal("0"), "holdings": [],
        "defi_positions": [], "total_assets": Decimal("0"),
        "total_debts": Decimal("0"),
    }

    try:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _scrape_one(addr: str):
            async with semaphore:
                try:
                    data = await scraper.scrape_portfolio(addr)
                    results[addr] = data
                except Exception:
                    logger.warning("Failed to scrape %s", addr[:10], exc_info=True)
                    results[addr] = empty.copy()

        await asyncio.gather(*[_scrape_one(addr) for addr in addresses])

        # Log summary
        success = sum(1 for d in results.values() if float(d.get("net_worth", 0)) > 0)
        logger.info("Solana scrape complete: %d/%d addresses returned data", success, len(addresses))
    finally:
        await scraper.close()

    return results

"""Portfolio service layer - core data fetching and aggregation logic."""

import asyncio
import json as _json
import logging
import time
from decimal import Decimal
from pathlib import Path

import httpx

from .adapters.debank import DeBankAdapter, TOKEN_CONVERT

# Cache file for EVM addresses with active DeFi borrows.
# Written by full portfolio fetch, read by lightweight health check.
_BORROW_CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "borrow_addresses.json"

# Cache file for EVM addresses with NFT holdings.
# Written by full portfolio fetch, used to skip NFT scanning for empty addresses.
_NFT_CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "nft_addresses.json"

logger = logging.getLogger(__name__)

# Runtime cache for CoinGecko search results (symbol -> coingecko id)
_search_cache: dict[str, str | None] = {}

from .adapters.exchanges.binance import BinanceAdapter
from .adapters.exchanges.bybit import BybitAdapter
from .adapters.exchanges.bitget import BitgetAdapter
from .adapters.exchanges.okx import OKXAdapter
from .adapters.jupiter import JupiterAdapter
from .config import Config

# Case-insensitive TOKEN_CONVERT lookup
_TOKEN_CONVERT_UPPER = {k.upper(): v for k, v in TOKEN_CONVERT.items()}


def _normalize_symbol(symbol: str) -> str:
    """Normalize symbol via TOKEN_CONVERT (case-insensitive)."""
    return _TOKEN_CONVERT_UPPER.get(symbol.upper(), symbol)


async def _get_coingecko_prices(symbols: list[str]) -> dict[str, Decimal]:
    """Get prices from CoinGecko by symbol."""
    SYMBOL_TO_ID = {
        "ETH": "ethereum", "WETH": "ethereum", "BTC": "bitcoin", "WBTC": "wrapped-bitcoin",
        "USDT": "tether", "USDC": "usd-coin", "DAI": "dai", "SOL": "solana",
        "BNB": "binancecoin", "MATIC": "matic-network", "AVAX": "avalanche-2",
        "ARB": "arbitrum", "OP": "optimism", "LINK": "chainlink", "UNI": "uniswap",
        "AAVE": "aave", "DOGE": "dogecoin", "XRP": "ripple", "ADA": "cardano",
        "DOT": "polkadot", "SHIB": "shiba-inu", "LTC": "litecoin", "ATOM": "cosmos",
        "ENA": "ethena", "PENDLE": "pendle", "EIGEN": "eigenlayer",
        "ALT": "altlayer", "ZKP": "panther",
        "CLOUD": "sanctum-2", "AO": "ao-computer", "PERP": "perpetual-protocol", "STETH": "staked-ether", "stETH": "staked-ether",
        "EUR": "tether-eurt",
        "LISTA": "lista-dao", "ASTER": "astar", "SHELL": "shell-protocol",
        "SOLV": "solv-protocol", "BERA": "berachain", "USD1": "usd1",
        "BUSD": "binance-usd", "TUSD": "true-usd", "FDUSD": "first-digital-usd",
        "USDE": "ethena-usde", "FRAX": "frax", "LUSD": "liquity-usd",
        "BGB": "bitget-token", "IP": "story-protocol", "BABY": "babylonchain",
        "HUMA": "huma-finance", "SWELL": "swell-network", "FUEL": "fuel-network",
        "XION": "xion-2", "WLFI": "world-liberty-financial",
        "RON": "ronin", "JUP": "jupiter-exchange-solana", "JLP": "jupiter-perpetuals-liquidity-provider-token",
        "KMNO": "kamino", "RDNT": "radiant-capital", "JTO": "jito-governance-token",
        "CARDS": "collector-crypt", "DG": "degate",
        "LINEA": "linea", "SOPH": "sophon", "FOGO": "fogo",
    }

    prices = {
        "USDT": Decimal("1"), "USDC": Decimal("1"), "USD": Decimal("1"),
        "DAI": Decimal("1"), "BUSD": Decimal("1"), "TUSD": Decimal("1"),
        "FDUSD": Decimal("1"), "USD1": Decimal("1"), "USDE": Decimal("1"),
        "CASH": Decimal("1"), "PYUSD": Decimal("1"),
    }

    ids_to_fetch = set()
    symbol_to_id = {}
    symbols_to_search: list[str] = []
    for symbol in symbols:
        if symbol in prices:
            continue
        cg_id = SYMBOL_TO_ID.get(symbol.upper())
        if cg_id:
            ids_to_fetch.add(cg_id)
            symbol_to_id[symbol.upper()] = cg_id
        else:
            symbols_to_search.append(symbol)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Auto-search unknown symbols via CoinGecko /search API
        # Rate limit: max 5 searches per run, 1.5s delay between to avoid 429
        if symbols_to_search:
            search_count = 0
            max_searches = 5  # CoinGecko free tier: ~10-30 req/min
            for symbol in symbols_to_search:
                upper = symbol.upper()
                # Check runtime search cache first
                if upper in _search_cache:
                    cached = _search_cache[upper]
                    if cached is not None:
                        ids_to_fetch.add(cached)
                        symbol_to_id[upper] = cached
                    continue
                if search_count >= max_searches:
                    logger.info("CoinGecko search limit reached, skipping remaining %d symbols",
                                len(symbols_to_search) - search_count)
                    break
                try:
                    if search_count > 0:
                        await asyncio.sleep(1.5)  # Rate limit delay
                    resp = await client.get(
                        "https://api.coingecko.com/api/v3/search",
                        params={"query": symbol},
                    )
                    if resp.status_code == 429:
                        logger.info("CoinGecko rate limited, stopping search")
                        break
                    resp.raise_for_status()
                    search_count += 1
                    coins = resp.json().get("coins", [])
                    # Only use search result if the symbol matches exactly
                    matched = [c for c in coins if c.get("symbol", "").upper() == upper]
                    if matched:
                        cg_id = matched[0]["id"]
                        _search_cache[upper] = cg_id
                        ids_to_fetch.add(cg_id)
                        symbol_to_id[upper] = cg_id
                        logger.info("CoinGecko search: %s -> %s", symbol, cg_id)
                    else:
                        _search_cache[upper] = None
                        logger.warning("CoinGecko search: no results for %s", symbol)
                except Exception:
                    _search_cache[upper] = None
                    logger.warning("CoinGecko search failed for %s", symbol, exc_info=True)

        if not ids_to_fetch:
            return prices

        # Delay after searches to avoid 429 on price fetch
        if symbols_to_search:
            await asyncio.sleep(2.0)

        try:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ",".join(ids_to_fetch), "vs_currencies": "usd"},
            )
            if resp.status_code == 429:
                logger.warning("CoinGecko price API rate limited, retrying after 5s...")
                await asyncio.sleep(5.0)
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": ",".join(ids_to_fetch), "vs_currencies": "usd"},
                )
            resp.raise_for_status()
            data = resp.json()

            for symbol, cg_id in symbol_to_id.items():
                if cg_id in data and "usd" in data[cg_id]:
                    prices[symbol] = Decimal(str(data[cg_id]["usd"]))
        except Exception:
            logger.warning("Failed to fetch CoinGecko prices", exc_info=True)

    return prices


async def _get_binance_prices() -> dict[str, Decimal]:
    """Get current prices from Binance."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get("https://api.binance.com/api/v3/ticker/price")
        resp.raise_for_status()
        prices = {}
        for item in resp.json():
            symbol = item["symbol"]
            price = Decimal(item["price"])
            if symbol.endswith("USDT"):
                base = symbol[:-4]
                prices[base] = price
        prices["USDT"] = Decimal("1")
        prices["USD"] = Decimal("1")
        return prices


async def _get_all_prices(symbols: list[str]) -> dict[str, Decimal]:
    """Get prices from multiple sources (CoinGecko + Binance fallback)."""
    prices = await _get_coingecko_prices(symbols)

    missing = [s for s in symbols if s not in prices or prices.get(s, Decimal("0")) == 0]
    if missing:
        binance_prices = await _get_binance_prices()
        for symbol in missing:
            if symbol in binance_prices:
                prices[symbol] = binance_prices[symbol]

    return prices


# Position types whose aggregate `value` is not otherwise counted via
# supply/borrow/rewards token lists. Airdrop/Rewards feed pos["rewards"]
# (counted separately), so they are intentionally excluded here.
_SOLANA_VALUE_ONLY_TYPES = {"deposit", "staking", "farming", "swap_tips"}


def _normalize_solana_position(pos: dict) -> dict:
    """Synthesize one supply row for value-only Solana positions.

    jup.ag now reports some positions (e.g. Kamino Farming/Deposit) as an
    aggregate value with no itemized supply rows. The service merge only
    counts supply/borrow/rewards tokens and gates all_defi on
    `supply or borrow`, so such positions were dropped from
    source_breakdown['solana'] and from the report's DeFi section.

    Representing the aggregate as a single supply row lets the existing
    supply path count and render it. No-op when the position already has
    supply/borrow (prevents double counting).
    """
    if pos.get("supply") or pos.get("borrow"):
        return pos
    value = pos.get("value") or Decimal("0")
    if pos.get("type") in _SOLANA_VALUE_ONLY_TYPES and value > 0:
        label = pos.get("name") or pos.get("type") or pos.get("protocol") or "Position"
        pos = dict(pos)
        pos["supply"] = [{
            "symbol": label,
            "quantity": Decimal("0"),
            "price": Decimal("0"),
            "value": value,
        }]
    return pos


def _build_solana_defi_entry(pos: dict, address: str) -> dict:
    """Build a standardized DeFi position entry from a Solana scraper position."""
    supply_total = sum(float(s.get("value", 0)) for s in pos.get("supply", []))
    borrow_total = sum(float(b.get("value", 0)) for b in pos.get("borrow", []))
    health_rate = float(supply_total / borrow_total) if borrow_total > 0 else 999.0
    entry = {
        "protocol": pos.get("protocol", "unknown"),
        "chain": "solana",
        "name": pos.get("name", pos.get("type", "")),
        "address": address,
        "supply": [{"symbol": s["symbol"], "quantity": float(s.get("quantity", 0)), "value": float(s["value"])} for s in pos.get("supply", [])],
        "borrow": [{"symbol": b["symbol"], "quantity": float(b.get("quantity", 0)), "value": float(b["value"])} for b in pos.get("borrow", [])],
        "detail": {"health_rate": health_rate},
        "id": f"{pos.get('protocol', 'unknown')}:solana:{pos.get('name', '')}:{address[:6]}",
    }
    if pos.get("health_rate") is not None:
        entry["detail"]["ltv"] = pos["health_rate"]
    return entry


def _normalize_collection_name(name: str) -> str:
    """Clean Alchemy-polluted collection names so they match nftpricefloor.

    Alchemy returns names with invisible joiner/zero-width chars and
    single-letter spacing, e.g. "G r i f t e r s by X C O P Y" or
    "Bored Ape Yacht Club͏͏". Both break slug lookup and split one
    collection into several. This is deterministic cleanup, not fuzzy
    matching, and is idempotent.
    """
    import re

    if not name:
        return name
    # Strip invisible/zero-width chars: soft hyphen U+00AD, combining
    # grapheme joiner U+034F, ZW space/joiners U+200B-200D, bidi marks
    # U+200E/200F, word joiner U+2060, BOM/ZWNBSP U+FEFF.
    invisible = "­͏​‌‍‎‏⁠﻿"
    name = name.translate({ord(c): None for c in invisible})
    # Collapse single-letter-spaced runs: "X C O P Y" -> "XCOPY"
    name = re.sub(
        r"\b(?:[A-Za-z] ){2,}[A-Za-z]\b",
        lambda m: m.group(0).replace(" ", ""),
        name,
    )
    # Normalise whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


# NFT collection name -> nftpricefloor.com slug mapping (keys are the
# normalized collection name; see _normalize_collection_name)
_NFT_SLUG_MAP = {
    "Bored Ape Yacht Club": "bored-ape-yacht-club",
    "MutantApeYachtClub": "mutant-ape-yacht-club",
    "CRYPTOPUNKS": "cryptopunks",
    "CryptoPunks": "cryptopunks",
    "Pudgy Penguins": "pudgy-penguins",
    "Azuki": "azuki",
    "Milady Maker": "milady-maker",
    "DeGods": "degods",
    "Doodles": "doodles-official",
    "CloneX": "clonex",
    "Moonbirds": "proof-moonbirds",
    "World of Women": "world-of-women-nft",
    "Cool Cats": "cool-cats-nft",
    "Meebits": "meebits",
    "VeeFriends": "veefriends",
    "Chromie Squiggle": "chromie-squiggle-by-snowfro",
    "Autoglyphs": "autoglyphs",
    "Fidenza": "fidenza-by-tyler-hobbs",
    "Ringers": "ringers-by-dmitri-cherniak",
    # nftpricefloor uses irregular slugs unrelated to the collection name
    "Memelands MVP": "youtherealmvp",
    "XCOPY - MAX PAIN AND FRENS": "max-pain-and-frens-by-xcopy",
}


def _collection_to_slug(name: str) -> str:
    """Convert a collection name to nftpricefloor.com slug."""
    name = _normalize_collection_name(name)
    if name in _NFT_SLUG_MAP:
        return _NFT_SLUG_MAP[name]
    # Auto-generate slug: lowercase, replace spaces/special chars with hyphens
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


async def _fetch_nft_floor_prices(collections: list[str]) -> dict[str, Decimal]:
    """Fetch floor prices from nftpricefloor.com for given collection names.

    Returns: {collection_name: floor_price_usd}
    """
    prices: dict[str, Decimal] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for name in collections:
            slug = _collection_to_slug(name)
            try:
                resp = await client.get(
                    f"https://api-bff.nftpricefloor.com/projects/{slug}/details",
                )
                if resp.status_code != 200:
                    logger.debug("nftpricefloor: %s (%s) -> HTTP %d", name, slug, resp.status_code)
                    continue
                data = resp.json()
                floor_usd = data.get("floorPriceUsd")
                if floor_usd and float(floor_usd) > 0:
                    prices[name] = Decimal(str(floor_usd))
                    logger.info("nftpricefloor: %s -> $%.2f", name, float(floor_usd))
            except Exception:
                logger.debug("nftpricefloor: failed for %s", name, exc_info=True)
    return prices


def _resolve_nft_value(nft: dict, floor_prices: dict) -> float | None:
    """Resolve a single NFT's USD value.

    EVM: nftpricefloor.com is the ONLY source. Not indexed -> None (drop;
    treated as worthless / counterfeit).
    Solana: unchanged -- use nftpricefloor if present, else Helius estimate.

    Returns None when the NFT should be dropped entirely.
    """
    collection = nft.get("collection", "Unknown")
    if nft.get("chain") == "solana":
        if collection in floor_prices:
            return float(floor_prices[collection])
        return float(nft.get("estimated_value_usd", 0))
    # EVM
    if collection not in floor_prices:
        return None
    return float(floor_prices[collection])


def _aggregate_nft_holdings(
    all_raw_nfts: list[dict],
    floor_prices: dict,
    all_assets: dict,
    all_nft_holdings: list[dict],
    source_breakdown: dict,
) -> None:
    """Resolve, filter and aggregate raw NFTs into the snapshot structures.

    Drops NFTs whose resolved value is None (EVM, unindexed -> counterfeit)
    or below the $100 dust floor. Mutates all_assets, all_nft_holdings and
    source_breakdown in place (same semantics as the inline loop it replaces).
    """
    for nft in all_raw_nfts:
        # Normalize first so zero-width / letter-spaced name variants
        # aggregate into one asset and match floor_prices keys.
        collection = _normalize_collection_name(nft.get("collection", "Unknown"))
        nft["collection"] = collection
        resolved = _resolve_nft_value(nft, floor_prices)
        if resolved is None or resolved < 100:
            continue
        nft_val = resolved
        all_nft_holdings.append({
            "name": nft.get("name", "Unknown"),
            "collection": collection,
            "chain": nft.get("chain", "?"),
            "value": nft_val,
        })
        nft_symbol = f"[NFT]{collection}"
        nft_price = Decimal(str(nft_val))
        if nft_symbol in all_assets:
            all_assets[nft_symbol]["quantity"] += Decimal("1")
            all_assets[nft_symbol]["value"] += Decimal(str(nft_val))
            all_assets[nft_symbol]["price"] = (
                all_assets[nft_symbol]["value"] / all_assets[nft_symbol]["quantity"]
            )
        else:
            all_assets[nft_symbol] = {
                "quantity": Decimal("1"),
                "value": Decimal(str(nft_val)),
                "price": nft_price,
            }
        # Add to source breakdown
        chain = nft.get("chain", "")
        if chain == "solana":
            source_breakdown["solana"] += Decimal(str(nft_val))
        else:
            source_breakdown["evm"] += Decimal(str(nft_val))


class PortfolioService:
    """Core portfolio data service with caching."""

    DEFAULT_CACHE_TTL = 300  # 5 minutes

    def __init__(self, config: Config, cache_ttl: int | None = None):
        self.config = config
        self.cache_ttl = cache_ttl if cache_ttl is not None else self.DEFAULT_CACHE_TTL
        self._cache: dict | None = None
        self._cache_time: float = 0
        self._lock = asyncio.Lock()
        self._refreshing = False

    @property
    def is_refreshing(self) -> bool:
        return self._refreshing

    @property
    def cache_age(self) -> float | None:
        """Seconds since last cache update, or None if no cache."""
        if self._cache is None:
            return None
        return time.time() - self._cache_time

    async def get_portfolio(self, force_refresh: bool = False) -> dict:
        """Get portfolio data, using cache if available.

        Returns structured dict with:
            assets: dict[symbol, {quantity, value, price}]
            debts: dict[symbol, {quantity, value, price}]
            defi_positions: list[dict]
            total_assets: Decimal
            total_debts: Decimal
            net_worth: Decimal
            timestamp: float
            evm_addresses: list[str]
            solana_addresses: list[str]
            exchanges: list[str]
        """
        if not force_refresh and self._cache is not None:
            age = time.time() - self._cache_time
            if age < self.cache_ttl:
                return self._cache

        async with self._lock:
            # Double-check after acquiring lock
            if not force_refresh and self._cache is not None:
                age = time.time() - self._cache_time
                if age < self.cache_ttl:
                    return self._cache

            self._refreshing = True
            try:
                data = await self._fetch_all()
                self._cache = data
                self._cache_time = time.time()
                return data
            finally:
                self._refreshing = False

    async def get_health_data(self) -> dict:
        """Lightweight health-only fetch using cached borrow addresses.

        Only queries:
        - DeBank all_complex_protocol_list for addresses with known borrows (from cache)
        - Falls back to all EVM addresses if cache is missing or stale (>7d)
        - Solana DeFi positions (via scraper)
        - Exchange loan/margin health (Bybit, Binance, Bitget)

        Returns dict with defi_positions, exchange_health, cache_status.
        """
        all_defi: list[dict] = []
        exchange_health: dict[str, dict] = {}
        cache_status = "ok"  # ok | missing | stale | error

        # 1. DeBank: only query addresses with known borrows (from cache)
        #    Cache is written by full portfolio fetch (report/snapshot/portfolio).
        #    Fallback: scan all addresses if cache missing or stale (>48h).
        evm_addrs = []
        try:
            if _BORROW_CACHE.exists():
                cache = _json.loads(_BORROW_CACHE.read_text())
                age_hours = (time.time() - cache.get("updated_at", 0)) / 3600
                cached_addrs = cache.get("addresses", [])
                if age_hours < 168:  # 7 days
                    evm_addrs = cached_addrs
                    logger.info("Health check: using cached borrow addresses (%d addrs, %.1fh old)", len(evm_addrs), age_hours)
                else:
                    cache_status = "stale"
                    logger.warning("Health check: borrow cache stale (%.1fh >7d), falling back to all addresses", age_hours)
                    evm_addrs = self.config.evm_addresses
            else:
                cache_status = "missing"
                logger.warning("Health check: borrow cache missing! Full fetch may not have run. Falling back to all %d addresses.", len(self.config.evm_addresses))
                evm_addrs = self.config.evm_addresses
        except Exception:
            cache_status = "error"
            logger.warning("Health check: failed to read borrow cache, falling back to all", exc_info=True)
            evm_addrs = self.config.evm_addresses

        if evm_addrs and self.config.aggregators.use_debank:
            api_key = self.config.aggregators.get_debank_api_key()
            if api_key:
                debank_adapter = DeBankAdapter(api_key)
                try:
                    _sem = asyncio.Semaphore(5)

                    async def _fetch_protocols(addr: str):
                        async with _sem:
                            resp = await debank_adapter._client.get(
                                f"{debank_adapter.BASE_URL}/user/all_complex_protocol_list",
                                params={"id": addr.lower()},
                            )
                            resp.raise_for_status()
                            return addr, resp.json()

                    tasks = [_fetch_protocols(addr) for addr in evm_addrs]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for res in results:
                        if isinstance(res, Exception):
                            logger.warning("Health check protocol fetch failed: %s", res)
                            continue
                        addr, protocols = res
                        for protocol in protocols:
                            for item in protocol.get("portfolio_item_list", []):
                                detail = item.get("detail", {})
                                borrows = detail.get("borrow_token_list", [])
                                if not borrows:
                                    continue
                                borrow_val = sum(
                                    b.get("amount", 0) * b.get("price", 0)
                                    for b in borrows
                                )
                                if borrow_val < 10:
                                    continue
                                supply_tokens = [
                                    {"symbol": t.get("symbol", "?"), "value": t.get("amount", 0) * t.get("price", 0)}
                                    for t in detail.get("supply_token_list", [])
                                ]
                                borrow_tokens = [
                                    {"symbol": t.get("symbol", "?"), "value": t.get("amount", 0) * t.get("price", 0)}
                                    for t in borrows
                                ]
                                all_defi.append({
                                    "protocol": protocol.get("name", "Unknown"),
                                    "chain": protocol.get("chain", ""),
                                    "name": item.get("name", ""),
                                    "address": addr,
                                    "supply": supply_tokens,
                                    "borrow": borrow_tokens,
                                    "detail": {
                                        "health_rate": Decimal(str(detail["health_rate"])) if detail.get("health_rate") is not None else None,
                                    },
                                    "id": f"{protocol.get('name', 'unknown')}:{protocol.get('chain', '')}:{item.get('name', '')}:{addr[:6]}",
                                })
                finally:
                    await debank_adapter.close()

        # 2. Exchange health (all exchanges — generic via get_margin_positions)
        for exchange_config in self.config.exchanges:
            ex_name = exchange_config.name.lower()
            adapter = exchange_config.create_adapter()
            if not adapter:
                continue
            try:
                positions = await adapter.get_margin_positions()
                for pos in positions:
                    if not pos.liabilities:
                        continue
                    total_debts = sum(float(l.quantity) for l in pos.liabilities)
                    if total_debts <= 0:
                        continue
                    all_symbols = list(set(
                        [a.symbol for a in pos.assets] +
                        [l.symbol for l in pos.liabilities]
                    ))
                    prices = await _get_all_prices(all_symbols)
                    asset_usd = sum(float(a.quantity) * float(prices.get(a.symbol, 0)) for a in pos.assets)
                    debt_usd = sum(float(l.quantity) * float(prices.get(l.symbol, 0)) for l in pos.liabilities)
                    if debt_usd > 10:
                        # Find main collateral for liquidation price
                        main_collateral = {}
                        if pos.assets:
                            top = max(pos.assets, key=lambda a: float(a.quantity) * float(prices.get(a.symbol, 0)))
                            top_val = float(top.quantity) * float(prices.get(top.symbol, 0))
                            if top_val > 0:
                                main_collateral = {
                                    "symbol": top.symbol,
                                    "amount": float(top.quantity),
                                    "value_usd": top_val,
                                }
                        exchange_health[ex_name] = {
                            "ltv": debt_usd / asset_usd if asset_usd > 0 else 1.0,
                            "collateral_usd": asset_usd,
                            "debt_usd": debt_usd,
                            "main_collateral": main_collateral,
                        }
            except Exception:
                logger.exception("Health check failed for %s", ex_name)
            finally:
                await adapter.close()

        # 3. Solana DeFi positions
        if self.config.solana_addresses:
            from .adapters.solana_scraper import scrape_solana_portfolios
            try:
                sol_results = await scrape_solana_portfolios(
                    self.config.solana_addresses, max_concurrent=1,
                )
                for address, sol_data in sol_results.items():
                    for pos in sol_data.get("defi_positions", []):
                        if not pos.get("borrow"):
                            continue
                        all_defi.append(_build_solana_defi_entry(pos, address))
            except Exception:
                logger.exception("Health check failed for Solana")

        return {
            "defi_positions": all_defi,
            "exchange_health": exchange_health,
            "cache_status": cache_status,
        }

    async def _fetch_all(self) -> dict:
        """Fetch portfolio data from all configured sources."""
        all_assets: dict[str, dict] = {}
        all_debts: dict[str, dict] = {}
        all_defi: list[dict] = []
        all_nft_holdings: list[dict] = []
        wallet_breakdown: list[dict] = []  # per-address breakdown
        source_breakdown: dict[str, Decimal] = {
            "evm": Decimal("0"),
            "exchanges": Decimal("0"),
            "solana": Decimal("0"),
        }
        exchange_breakdown: dict[str, dict[str, Decimal]] = {}
        exchange_health: dict[str, dict] = {}  # exchange loan health info

        def add_asset(symbol: str, quantity: Decimal, price: Decimal, value: Decimal):
            base = _TOKEN_CONVERT_UPPER.get(symbol.upper(), symbol)
            if base not in all_assets:
                all_assets[base] = {"quantity": Decimal("0"), "value": Decimal("0"), "price": price}
            all_assets[base]["quantity"] += quantity
            all_assets[base]["value"] += value
            if symbol == base and price > 0:
                all_assets[base]["price"] = price

        debank_units = None

        # 1. Fetch DeBank data (EVM chains)
        if self.config.aggregators.use_debank:
            api_key = self.config.aggregators.get_debank_api_key()
            if api_key and self.config.evm_addresses:
                debank_adapter = DeBankAdapter(api_key)
                try:
                    # Parallel fetch all EVM addresses (5 concurrent max)
                    _sem = asyncio.Semaphore(5)

                    async def _fetch_evm(addr):
                        async with _sem:
                            portfolio = await debank_adapter.get_portfolio(addr)
                            return addr, portfolio

                    _tasks = [_fetch_evm(addr) for addr in self.config.evm_addresses]
                    _results = await asyncio.gather(*_tasks, return_exceptions=True)

                    for _res in _results:
                        if isinstance(_res, Exception):
                            continue
                        address, portfolio = _res
                        try:
                            source_breakdown["evm"] += portfolio.get("total_assets", Decimal("0"))

                            # Per-wallet breakdown
                            wallet_assets = []
                            for sym, data in portfolio["assets"].items():
                                if data["value"] >= 10:
                                    wallet_assets.append({"symbol": sym, "quantity": data["quantity"], "value": data["value"], "price": data["price"]})
                            wallet_debts_list = []
                            for sym, data in portfolio["debts"].items():
                                if data["value"] >= 10:
                                    wallet_debts_list.append({"symbol": sym, "quantity": data["quantity"], "value": data["value"], "price": data["price"]})
                            wallet_total = portfolio.get("total_assets", Decimal("0")) - portfolio.get("total_debts", Decimal("0"))
                            wallet_breakdown.append({"address": address, "source": "evm", "total": wallet_total, "assets": wallet_assets, "debts": wallet_debts_list, "defi": [p for p in portfolio.get("defi_positions", [])]})

                            for symbol, data in portfolio["assets"].items():
                                if symbol not in all_assets:
                                    all_assets[symbol] = {"quantity": Decimal("0"), "value": Decimal("0"), "price": data["price"]}
                                all_assets[symbol]["quantity"] += data["quantity"]
                                all_assets[symbol]["value"] += data["value"]

                            for symbol, data in portfolio["debts"].items():
                                if symbol not in all_debts:
                                    all_debts[symbol] = {"quantity": Decimal("0"), "value": Decimal("0"), "price": data["price"]}
                                all_debts[symbol]["quantity"] += data["quantity"]
                                all_debts[symbol]["value"] += data["value"]

                            for pos in portfolio.get("defi_positions", []):
                                pos["address"] = address
                                pos.setdefault(
                                    "id",
                                    f"{pos.get('protocol', 'unknown')}:{pos.get('chain', 'unknown')}:{pos.get('name', '')}:{address[:6]}",
                                )
                                all_defi.append(pos)
                        except Exception:
                            logger.warning("Failed to process EVM address %s", address[:10], exc_info=True)

                    # Save addresses with active borrows for lightweight health check
                    borrow_addrs = list({
                        pos.get("address", "")
                        for pos in all_defi
                        if any(
                            float(t.get("value", 0)) >= 10
                            for t in pos.get("borrow", [])
                        )
                    })
                    try:
                        _BORROW_CACHE.parent.mkdir(parents=True, exist_ok=True)
                        _BORROW_CACHE.write_text(_json.dumps({
                            "addresses": borrow_addrs,
                            "updated_at": time.time(),
                        }))
                        logger.info("Borrow address cache updated: %d addresses", len(borrow_addrs))
                    except Exception:
                        logger.warning("Failed to write borrow address cache", exc_info=True)

                    # Fetch API units balance after all DeBank calls
                    try:
                        debank_units = await debank_adapter.get_units_balance()
                    except Exception:
                        debank_units = None
                        logger.warning("Failed to fetch DeBank units balance", exc_info=True)

                finally:
                    await debank_adapter.close()

        # 2. Collect exchange assets (without prices)
        exchange_assets: list[tuple[str, Decimal, str]] = []
        exchange_debts: list[tuple[str, Decimal, str]] = []
        # Store loan positions per exchange for health calculation after pricing
        _exchange_loan_positions: dict[str, list] = {}  # ex_name -> [Position]

        for exchange_config in self.config.exchanges:
            if exchange_config.name.lower() == "binance":
                api_key = exchange_config.get_api_key()
                api_secret = exchange_config.get_api_secret()
                if api_key and api_secret:
                    adapter = BinanceAdapter(api_key, api_secret)
                    try:
                        for asset in await adapter.get_spot_balances():
                            exchange_assets.append((asset.symbol, asset.quantity, "binance:spot"))
                        for position in await adapter.get_earn_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "binance:earn"))
                        for position in await adapter.get_futures_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "binance:futures"))
                        for position in await adapter.get_margin_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "binance:margin"))
                            for liability in (position.liabilities or []):
                                exchange_debts.append((liability.symbol, liability.quantity, "binance:margin"))
                            if position.liabilities:
                                _exchange_loan_positions.setdefault("binance", []).append(position)
                    finally:
                        await adapter.close()

            elif exchange_config.name.lower() == "bybit":
                api_key = exchange_config.get_api_key()
                api_secret = exchange_config.get_api_secret()
                if api_key and api_secret:
                    adapter = BybitAdapter(api_key, api_secret)
                    try:
                        for asset in await adapter.get_spot_balances():
                            exchange_assets.append((asset.symbol, asset.quantity, "bybit:unified"))
                        for position in await adapter.get_earn_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "bybit:earn"))
                        for position in await adapter.get_crypto_loan_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "bybit:loan"))
                            for liability in (position.liabilities or []):
                                exchange_debts.append((liability.symbol, liability.quantity, "bybit:loan"))
                        # Get Bybit crypto loan health (LTV)
                        try:
                            loan_data = await adapter._signed_request("GET", "/v5/crypto-loan-common/position", {})
                            if loan_data.get("retCode") == 0:
                                result = loan_data.get("result", {})
                                ltv = result.get("ltv")
                                if ltv:
                                    # Extract main collateral token for liquidation price display
                                    collateral_list = result.get("collateralList", [])
                                    main_collateral = {}
                                    if collateral_list:
                                        top = max(collateral_list, key=lambda c: float(c.get("amountUSD", 0)))
                                        main_collateral = {
                                            "symbol": top.get("currency", "?"),
                                            "amount": float(top.get("amount", 0)),
                                            "value_usd": float(top.get("amountUSD", 0)),
                                        }
                                    exchange_health["bybit"] = {
                                        "ltv": float(ltv),
                                        "collateral_usd": float(result.get("totalCollateral", 0)),
                                        "debt_usd": float(result.get("totalDebt", 0)),
                                        "main_collateral": main_collateral,
                                    }
                        except Exception:
                            logger.warning("Failed to fetch Bybit loan health", exc_info=True)
                    finally:
                        await adapter.close()

            elif exchange_config.name.lower() == "bitget":
                api_key = exchange_config.get_api_key()
                api_secret = exchange_config.get_api_secret()
                passphrase = exchange_config.get_passphrase() or ""
                if api_key and api_secret:
                    adapter = BitgetAdapter(api_key, api_secret, passphrase)
                    try:
                        for asset in await adapter.get_spot_balances():
                            exchange_assets.append((asset.symbol, asset.quantity, "bitget:spot"))
                        for position in await adapter.get_earn_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "bitget:earn"))
                        for position in await adapter.get_futures_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "bitget:futures"))
                        for position in await adapter.get_margin_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "bitget:margin"))
                            for liability in position.liabilities:
                                exchange_debts.append((liability.symbol, liability.quantity, "bitget:margin"))
                    finally:
                        await adapter.close()

            elif exchange_config.name.lower() == "okx":
                api_key = exchange_config.get_api_key()
                api_secret = exchange_config.get_api_secret()
                passphrase = exchange_config.get_passphrase()
                if api_key and api_secret and passphrase:
                    adapter = OKXAdapter(api_key, api_secret, passphrase)
                    try:
                        for asset in await adapter.get_spot_balances():
                            exchange_assets.append((asset.symbol, asset.quantity, "okx:spot"))
                        for position in await adapter.get_earn_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "okx:earn"))
                        for position in await adapter.get_futures_positions():
                            for asset in position.assets:
                                exchange_assets.append((asset.symbol, asset.quantity, "okx:perp"))
                    finally:
                        await adapter.close()

        # 3. Price exchange assets
        if exchange_assets or exchange_debts:
            all_symbols = list(set(s for s, _, _ in exchange_assets + exchange_debts))
            prices = await _get_all_prices(all_symbols)

            # Per-exchange token detail for wallet_breakdown
            ex_detail: dict[str, dict] = {}  # exchange -> {assets: [], debts: [], total: Decimal}

            for symbol, quantity, source in exchange_assets:
                # Try original, then normalized symbol for price
                price = prices.get(symbol, prices.get(_normalize_symbol(symbol), Decimal("0")))
                value = quantity * price
                if value >= 1:
                    # Convert stETH-like tokens to ETH equivalent quantity
                    norm = _normalize_symbol(symbol)
                    if norm == "ETH" and symbol.upper() != "ETH":
                        eth_price = prices.get("ETH", Decimal("0"))
                        if eth_price > 0:
                            equiv_qty = quantity * price / eth_price
                            add_asset("ETH", equiv_qty, eth_price, value)
                        else:
                            add_asset(symbol, quantity, price, value)
                    else:
                        add_asset(symbol, quantity, price, value)
                    exchange_name = source.split(":")[0]
                    exchange_breakdown.setdefault(exchange_name, {"assets": Decimal("0"), "debts": Decimal("0")})
                    exchange_breakdown[exchange_name]["assets"] += value
                    source_breakdown["exchanges"] += value
                    # Track per-exchange detail
                    ex_detail.setdefault(exchange_name, {"assets": [], "debts": [], "total": Decimal("0")})
                    ex_detail[exchange_name]["assets"].append({"symbol": symbol, "quantity": quantity, "value": value, "price": price, "source": source})
                    ex_detail[exchange_name]["total"] += value

            for symbol, quantity, source in exchange_debts:
                price = prices.get(symbol, prices.get(_normalize_symbol(symbol), Decimal("0")))
                value = quantity * price
                base = _TOKEN_CONVERT_UPPER.get(symbol.upper(), symbol)
                if value >= 1:
                    if base not in all_debts:
                        all_debts[base] = {"quantity": Decimal("0"), "value": Decimal("0"), "price": price}
                    all_debts[base]["quantity"] += quantity
                    all_debts[base]["value"] += value
                    exchange_name = source.split(":")[0]
                    exchange_breakdown.setdefault(exchange_name, {"assets": Decimal("0"), "debts": Decimal("0")})
                    exchange_breakdown[exchange_name]["debts"] += value
                    ex_detail.setdefault(exchange_name, {"assets": [], "debts": [], "total": Decimal("0")})
                    ex_detail[exchange_name]["debts"].append({"symbol": symbol, "quantity": quantity, "value": value, "price": price, "source": source})
                    ex_detail[exchange_name]["total"] -= value

            # Add exchange wallets to wallet_breakdown
            for ex_name, detail in ex_detail.items():
                # Separate loan assets/debts from spot assets
                loan_positions = _exchange_loan_positions.get(ex_name, [])
                loan_asset_ids = set()
                loan_debt_ids = set()
                ex_defi: list[dict] = []
                for pos in loan_positions:
                    if not pos.liabilities:
                        continue
                    for a in pos.assets:
                        loan_asset_ids.add((a.symbol, a.source))
                    for l in pos.liabilities:
                        loan_debt_ids.add((l.symbol, l.source))
                    # Build defi-like entry for display
                    supply = []
                    for a in pos.assets:
                        val = float(a.quantity) * float(prices.get(a.symbol, 0))
                        if val >= 10:
                            supply.append({"symbol": a.symbol, "quantity": float(a.quantity), "amount": float(a.quantity), "value": val})
                    borrow = []
                    for l in pos.liabilities:
                        val = float(l.quantity) * float(prices.get(l.symbol, 0))
                        if val >= 10:
                            borrow.append({"symbol": l.symbol, "quantity": float(l.quantity), "amount": float(l.quantity), "value": val})
                    if supply or borrow:
                        supply_val = sum(s["value"] for s in supply)
                        borrow_val = sum(b["value"] for b in borrow)
                        hr = supply_val / borrow_val if borrow_val > 0 else 999.0
                        ex_defi.append({
                            "protocol": ex_name.title() + " Loan",
                            "name": pos.id.split(":")[-1] if ":" in pos.id else "",
                            "health_rate": hr,
                            "supply": supply,
                            "borrow": borrow,
                        })

                # Filter out loan collateral/debt from spot display
                spot_assets = [a for a in detail["assets"]
                               if a["value"] >= 10 and (a["symbol"], a.get("source", "")) not in loan_asset_ids]
                spot_debts = [d for d in detail["debts"]
                              if d["value"] >= 10 and (d["symbol"], d.get("source", "")) not in loan_debt_ids]

                wallet_breakdown.append({
                    "address": ex_name,
                    "source": "exchange",
                    "total": detail["total"],
                    "assets": [{"symbol": a["symbol"], "quantity": a["quantity"], "value": a["value"], "price": a["price"]} for a in spot_assets],
                    "debts": [{"symbol": d["symbol"], "quantity": d["quantity"], "value": d["value"], "price": d["price"]} for d in spot_debts],
                    "defi": ex_defi,
                })

        # 3b. Compute exchange loan health from stored positions (needs prices)
        for ex_name, loan_positions in _exchange_loan_positions.items():
            if ex_name in exchange_health:
                continue  # already set (e.g. Bybit via dedicated API)
            for pos in loan_positions:
                if not pos.liabilities:
                    continue
                pos_asset_usd = sum(float(a.quantity) * float(prices.get(a.symbol, 0)) for a in pos.assets)
                pos_debt_usd = sum(float(l.quantity) * float(prices.get(l.symbol, 0)) for l in pos.liabilities)
                if pos_debt_usd > 10:
                    main_collateral = {}
                    if pos.assets:
                        top = max(pos.assets, key=lambda a: float(a.quantity) * float(prices.get(a.symbol, 0)))
                        top_val = float(top.quantity) * float(prices.get(top.symbol, 0))
                        if top_val > 0:
                            main_collateral = {
                                "symbol": top.symbol,
                                "amount": float(top.quantity),
                                "value_usd": top_val,
                            }
                    exchange_health[ex_name] = {
                        "ltv": pos_debt_usd / pos_asset_usd if pos_asset_usd > 0 else 1.0,
                        "collateral_usd": pos_asset_usd,
                        "debt_usd": pos_debt_usd,
                        "main_collateral": main_collateral,
                    }

        # 4. Fetch Solana data (via jup.ag/portfolio scraper for full DeFi coverage)
        if self.config.solana_addresses:
            from .adapters.solana_scraper import scrape_solana_portfolios

            try:
                sol_results = await scrape_solana_portfolios(
                    self.config.solana_addresses, max_concurrent=1,
                )

                for address, sol_data in sol_results.items():
                    sol_wallet_assets = []
                    sol_wallet_debts = []

                    # Merge holdings into global assets
                    for holding in sol_data.get("holdings", []):
                        symbol = holding["symbol"]
                        quantity = holding["quantity"]
                        price = holding["price"]
                        value = holding["value"]
                        if value >= 1:
                            base = _normalize_symbol(symbol)
                            add_asset(base, quantity, price, value)
                            source_breakdown["solana"] += value
                            if value >= 10:
                                sol_wallet_assets.append({
                                    "symbol": base, "quantity": quantity,
                                    "value": value, "price": price,
                                })

                    # Merge DeFi positions
                    for pos in sol_data.get("defi_positions", []):
                        # Value-only positions (e.g. Kamino Farming/Deposit)
                        # get a synthetic supply row so the path below counts
                        # them in source_breakdown and includes them in all_defi.
                        pos = _normalize_solana_position(pos)
                        # Add supply assets to global assets
                        for s in pos.get("supply", []):
                            if s["value"] >= 1:
                                base = _normalize_symbol(s["symbol"])
                                add_asset(base, s["quantity"], s["price"], s["value"])
                                source_breakdown["solana"] += s["value"]

                        # Add borrow to global debts
                        for b in pos.get("borrow", []):
                            if b["value"] >= 1:
                                base = _normalize_symbol(b["symbol"])
                                if base not in all_debts:
                                    all_debts[base] = {"quantity": Decimal("0"), "value": Decimal("0"), "price": b["price"]}
                                all_debts[base]["quantity"] += b["quantity"]
                                all_debts[base]["value"] += b["value"]
                                sol_wallet_debts.append({
                                    "symbol": base, "quantity": b["quantity"],
                                    "value": b["value"], "price": b["price"],
                                })

                        # Add rewards value to assets
                        for r in pos.get("rewards", []):
                            if r["value"] >= 1:
                                base = _normalize_symbol(r["symbol"])
                                add_asset(base, r["quantity"], Decimal("0"), r["value"])
                                source_breakdown["solana"] += r["value"]

                        if pos.get("supply") or pos.get("borrow"):
                            all_defi.append(_build_solana_defi_entry(pos, address))

                    sol_total = sol_data.get("net_worth", Decimal("0"))
                    wallet_breakdown.append({
                        "address": address, "source": "solana",
                        "total": sol_total, "assets": sol_wallet_assets,
                        "debts": sol_wallet_debts, "defi": sol_data.get("defi_positions", []),
                    })

            except Exception:
                logger.exception("Failed to scrape Solana portfolios")

        # 5. NFT discovery via Alchemy (EVM) + Helius (Solana)
        alchemy_key = self.config.aggregators.get_alchemy_api_key()
        helius_key = self.config.aggregators.get_helius_api_key()

        nft_tasks = []
        if alchemy_key and self.config.evm_addresses:
            from .adapters.alchemy_nft import get_nfts_for_addresses
            nft_tasks.append(("evm", get_nfts_for_addresses(alchemy_key, self.config.evm_addresses)))
        if helius_key and self.config.solana_addresses:
            from .adapters.helius_nft import get_solana_nfts
            nft_tasks.append(("solana", get_solana_nfts(helius_key, self.config.solana_addresses)))

        if nft_tasks:
            nft_results = await asyncio.gather(*[t[1] for t in nft_tasks], return_exceptions=True)
            # Collect all NFT collections for nftpricefloor lookup
            _all_nft_collections: set[str] = set()
            all_raw_nfts: list[dict] = []
            for i, (source, _) in enumerate(nft_tasks):
                result = nft_results[i]
                if isinstance(result, Exception):
                    logger.warning("NFT fetch failed for %s: %s", source, result)
                    continue
                for nft in result.get("nfts", []):
                    nft["collection"] = _normalize_collection_name(
                        nft.get("collection", "Unknown")
                    )
                    all_raw_nfts.append(nft)
                    _all_nft_collections.add(nft["collection"])

            # Override with nftpricefloor.com prices where available
            _nft_floor_prices: dict[str, Decimal] = {}
            if _all_nft_collections:
                try:
                    _nft_floor_prices = await _fetch_nft_floor_prices(list(_all_nft_collections))
                except Exception:
                    logger.warning("Failed to fetch NFT floor prices", exc_info=True)

            _aggregate_nft_holdings(
                all_raw_nfts, _nft_floor_prices,
                all_assets, all_nft_holdings, source_breakdown,
            )

        total_assets = sum(d["value"] for d in all_assets.values())
        total_debts = sum(d["value"] for d in all_debts.values())

        return {
            "assets": all_assets,
            "debts": all_debts,
            "defi_positions": all_defi,
            "nft_holdings": all_nft_holdings,
            "total_assets": total_assets,
            "total_debts": total_debts,
            "net_worth": total_assets - total_debts,
            "timestamp": time.time(),
            "evm_addresses": self.config.evm_addresses,
            "solana_addresses": self.config.solana_addresses,
            "exchanges": [e.name for e in self.config.exchanges],
            "source_breakdown": source_breakdown,
            "exchange_breakdown": exchange_breakdown,
            "wallet_breakdown": wallet_breakdown,
            "exchange_health": exchange_health,
            "debank_units": debank_units,
        }

# Portfolio Tracker

Multi-chain net-worth and position-allocation reporting. Aggregates
holdings across EVM wallets (via DeBank), Solana wallets (via the
jup.ag portfolio page), and centralized exchanges, including DeFi
positions and NFTs, and pushes daily/weekly reports to Discord.

## Features

- **EVM**: tokens + DeFi via DeBank (56+ chains).
- **Solana**: tokens + DeFi (Kamino, Jupiter, etc.) scraped from
  jup.ag/portfolio with a Playwright browser.
- **Exchanges**: Binance, Bybit, Bitget, OKX balances.
- **NFTs**: floor-priced via nftpricefloor.com (EVM).
- **Reports**: net worth, holdings table, DeFi positions, and a
  **position-allocation breakdown** — BTC / ETH / SOL / BNB / Alt /
  Stable. LSTs and wrapped tokens fold into their base coin, JLP is
  split by live pool composition, and stablecoin Pendle PTs count as
  stable.
- **Alerts**: DeFi liquidation-health monitoring.
- **Delivery**: one-way Discord webhook (no bot); schedulable via
  launchd / cron.
- Snapshots stored in a local SQLite DB for week-over-week comparison.

## Setup

Requires Python ≥ 3.11.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium          # for the Solana scraper

cp .env.example .env                 # fill in API keys / webhook
cp config.example.yaml config.yaml   # fill in your wallet addresses
```

`.env`, `config.yaml`, the SQLite DB and `data/` are git-ignored — your
addresses and keys never enter version control.

The Solana scraper drives a real Chrome with a persistent profile so a
Cloudflare Turnstile challenge only needs to be solved once manually
(run any Solana command interactively the first time).

## Usage

```bash
python -m portfolio_tracker <command>
```

| Command | Description |
|---|---|
| `report` | Print the current report to stdout |
| `weekly-report` | Print the weekly report (with WoW comparison) |
| `report-send` | Generate + send the daily report to Discord |
| `weekly-send` | Generate + send the weekly report to Discord |
| `alert-send [threshold]` | Health check → Discord (only if alerting) |
| `check-health [threshold]` | DeFi liquidation-health check (stdout) |
| `preview [current\|daily\|weekly]` | Re-render last snapshot, no API calls |
| `snapshot` | Fetch and store a snapshot only |
| `portfolio` / `assets` / `defi` / `exchange` | Ad-hoc views |
| `history [days]` | Net-worth change over time |
| `breakdown [min_value]` | Per-source asset breakdown |
| `add-address` / `remove-address` / `list-addresses` | Manage wallets |
| `add-manual` / `list-manual` / `remove-manual` | Manual off-chain entries |
| `set-key <name>` | Save an API key to the macOS Keychain |

## Scheduling (macOS launchd)

`scripts/` contains shell wrappers and launchd plists under
`scripts/launchd/` for unattended delivery:

| Schedule | Script |
|---|---|
| Daily | `scripts/daily-report.sh` |
| Weekly (Monday) | `scripts/weekly-report.sh` |
| Hourly | `scripts/health-check.sh` |

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
launchctl kickstart -k gui/$(id -u)/<label>        # run now (test)
launchctl bootout   gui/$(id -u)/<label>           # unload
```

## Configuration

- `.env` — API keys (DeBank, exchanges) and the Discord webhook URL.
  Keys may also be stored in the macOS Keychain via `set-key`.
- `config.yaml` — tracked `evm_addresses` / `solana_addresses`,
  enabled chains, aggregators, pricing, report settings.

See `.env.example` and `config.example.yaml` for the full key set.

## Tests

```bash
python -m pytest -q
```

Tests run fully offline against synthetic fixtures — no network and no
real portfolio data.

## License

MIT.

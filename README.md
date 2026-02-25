# BothMarkets — Cross-Platform Prediction Market Arbitrage Scanner

BothMarkets scans [Kalshi](https://kalshi.com) and [Polymarket](https://polymarket.com) every 2 seconds for live prices on matched binary markets. When the combined cost to buy YES on one platform and NO on the other falls below 100 cents, the spread is guaranteed profit regardless of how the event resolves.

---

## How It Works

Both platforms offer binary contracts (YES/NO) on the same real-world events. If you can buy:

- **Kalshi YES** at 48c + **Polymarket NO** at 49c = 97c combined

Then one of those contracts always pays out 100c, locking in a 3c profit per share. BothMarkets finds these opportunities automatically and, when credentials are configured, executes both legs atomically.

**Strategy A:** Buy Kalshi YES + Buy Polymarket NO when `kalshi_yes_ask + poly_no_ask < 100`

**Strategy B:** Buy Kalshi NO + Buy Polymarket YES when `kalshi_no_ask + poly_yes_ask < 100`

**Spread = 100c - combined cost = guaranteed profit per share traded**

---

## Architecture

### Two-Speed Loop (`scanner/runner.py`)

| Loop | Interval | What it does |
|------|----------|--------------|
| Market refresh (slow) | Every 2 hours | Fetches all open Kalshi + Polymarket markets, runs 6-criteria matching to build the matched pairs list |
| Price poll (fast) | Every 2 seconds | Fetches live prices for all matched pairs in parallel, evaluates Strategy A and B, logs and executes opportunities |

### Module Overview

| File | Purpose |
|------|---------|
| `scanner/runner.py` | Main entry point — two-speed loop, logging setup, executor initialization |
| `scanner/kalshi_client.py` | Fetches and normalizes Kalshi markets; polls live prices and orderbook depth |
| `scanner/poly_client.py` | Fetches Polymarket markets via Gamma API; polls CLOB prices per token |
| `scanner/market_matcher.py` | Matches Kalshi and Polymarket markets by sport, team, opponent, date, subtype, and map number |
| `scanner/opportunity_finder.py` | Detects arbitrage from matched pair prices; tiers opportunities by spread size |
| `scanner/arb_executor.py` | Executes two-leg trades; handles position sizing, cooldowns, and Kalshi unwind on Polymarket failure |
| `scanner/kalshi_trader.py` | Kalshi order placement via RSA-PS256 signed REST API |
| `scanner/poly_trader.py` | Polymarket order placement via `py-clob-client` (sig_type=2 proxy mode) |
| `scanner/market_matcher.py` | 6-criteria sports matching; 4-criteria crypto matching (disabled by default) |
| `scanner/models.py` | `NormalizedMarket`, `MatchedPair`, `Opportunity` dataclasses |
| `scanner/config.py` | All constants (timing, thresholds, env var names, API URLs) |

---

## Supported Market Types

### Sports (active)
Esports and traditional sports game-winner markets matched across both platforms:

**CS2, LoL, VALORANT, Dota 2, NBA, NFL, NHL, MLB, Soccer**

Matching uses 6 criteria — all must pass:
1. **Sport** — same sport code
2. **Team** — same normalized team name
3. **Opponent** — same normalized opponent (prevents wrong-matchup pairing)
4. **Date** — resolution times within ±1 hour
5. **Subtype** — map/game winner vs. series/match winner
6. **Map number** — when both markets specify a game or map number, they must agree

### Crypto (disabled by default)
BTC, ETH, SOL, XRP, DOGE price-level markets. Disabled via `CRYPTO_MATCHING_ENABLED = False` in `config.py` due to oracle mismatch (Kalshi uses CF Benchmarks BRTI; Polymarket uses Binance 1-min candle close) and a structural 5-hour gap in resolution times.

---

## Opportunity Tiers

Tiers are adjusted +0.5c above raw spread to account for cross-platform fund transfer fees:

| Tier | Spread |
|------|--------|
| Ultra High | > 5.5c |
| High | 2.5c – 5.5c |
| Mid | 1.5c – 2.5c |
| Low | 0.8c – 1.5c |

Opportunities below 0.8c (`MIN_SPREAD_CENTS`) are ignored.

---

## Setup

### Requirements

- Python 3.12+
- Dependencies: `httpx`, `python-dotenv`, `py-clob-client`, `cryptography`

```bash
pip install -e .
# or with dev tools:
pip install -e ".[dev]"
```

### Credentials (.env)

Create a `.env` file in the project root:

```env
# Kalshi — RSA key pair
KALSHI_API_KEY=<uuid>
KALSHI_API_SECRET=-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----

# Polymarket — Ethereum private key + CLOB API credentials
POLY_PRIVATE_KEY=<hex, no 0x prefix>
POLY_API_KEY=<uuid>
POLY_API_SECRET=<base64>
POLY_API_PASSPHRASE=<hex>
POLY_FUNDER=<Bot proxy wallet address>
```

The scanner runs in **scan-only mode** (no trades placed) if any credential is missing.

---

## Kalshi Setup

- Generate an RSA key pair in the Kalshi web UI under API settings.
- `KALSHI_API_KEY` — the UUID shown as the API key ID.
- `KALSHI_API_SECRET` — the RSA private key in PEM format. Escaped newlines (`\n`) in `.env` are handled automatically.
- Auth scheme: RSA-PS256. Signature message = `timestamp_ms + METHOD + path + ""` (body is always signed as an empty string, even for POST requests).

---

## Polymarket Setup

- `POLY_PRIVATE_KEY` — Ethereum private key for the signing wallet (hex, no `0x` prefix).
- `POLY_FUNDER` — the **Bot proxy wallet address** shown in Polymarket Settings → API Keys. This is **not** the signing key's address. The proxy wallet holds the USDC balance used for trading.
- `POLY_API_KEY` / `POLY_API_SECRET` / `POLY_API_PASSPHRASE` — CLOB API credentials. If left blank, they are auto-derived from `POLY_PRIVATE_KEY` using `derive_api_key(nonce=0)`.
- Signature mode: `sig_type=2` (proxy). `maker` = `POLY_FUNDER`, `signer` = private key address.
- Balance visible in the Polymarket web UI lives in the proxy wallet's off-chain CLOB ledger — no on-chain USDC deposit or MATIC is needed.
- Orders use FOK (Fill or Kill) type: they fill immediately or are cancelled.

---

## Trading Logic

### Position Sizing

For each opportunity, the number of contracts is calculated as:

```
units = min(
    floor(max_trade_usd / (k_price + p_price)),  # dollar cap
    kalshi_depth_at_best_ask,                     # Kalshi liquidity
    poly_depth_at_best_ask,                       # Polymarket liquidity
)
```

- Max total spend per trade: `$5.00` (configurable via `EXEC_MAX_TRADE_USD` in `config.py`)
- Polymarket minimum order: `$1.00` per leg (`EXEC_POLY_MIN_ORDER_USD`)
- If calculated units don't meet the Polymarket minimum, the trade is skipped

### Execution Order

1. Check Polymarket USDC balance — skip if below `$1.00`
2. Place Kalshi order (Leg 1)
3. Wait 0.5 seconds, then verify actual fill count — cancel resting remainder, adjust size if partial
4. Place Polymarket order sized to actual Kalshi fill (Leg 2)

### Safety Guards

| Guard | Behavior |
|-------|---------|
| **Balance check** | Reads Polymarket USDC balance before every trade. Skips if below `$1.00` to prevent a Kalshi-buys-but-Poly-fails loss cycle. |
| **Partial fill guard** | After placing the Kalshi order, queries actual fill count. If Kalshi partially filled, cancels the resting remainder and sizes the Polymarket leg to match the actual fill — preventing unhedged exposure. If zero contracts filled, aborts without touching Polymarket. |
| **Kalshi unwind** | If Leg 1 (Kalshi) fills but Leg 2 (Polymarket) fails, automatically sells the Kalshi position back at current bid. Retries up to 3 times. Status becomes `"unwound"` on success or `"partial_stuck"` on failure (requires manual intervention). |
| **Pair cooldown** | After each trade attempt, the pair is locked for 15 price cycles (~30 seconds). After a failed trade (unwind triggered), the cooldown is doubled. |
| **429 backoff** | On Kalshi API rate limit errors during market refresh, waits 30 seconds before retrying instead of hammering the API. |

### Execution Result Statuses

| Status | Meaning |
|--------|---------|
| `filled` | Both legs placed successfully; profit locked in |
| `skipped` | Trade not attempted (balance too low, no liquidity, insufficient units, pair on cooldown) |
| `unwound` | Kalshi filled, Polymarket failed; Kalshi position sold back successfully |
| `partial_stuck` | Kalshi filled, Polymarket failed; Kalshi unwind also failed — manual action required |
| `error` | Unexpected error during execution |

---

## Running

```bash
# Start the scanner (from the bothmarkets/ directory)
py -m scanner.runner

# Run tests
py -m pytest tests/ -v
```

The scanner loads `.env` automatically from the project root on startup.

---

## Output Files

| File | Contents |
|------|---------|
| `scanner.log` | All log output (debug + info for every component) |
| `opportunities.log` | Filtered log: matched pairs, arbitrage opportunities, and execution events only |
| `opportunities.json` | NDJSON — one JSON object per price cycle that found opportunities, with full opportunity details |

---

## Dev Server Config

The project includes `.claude/launch.json` (in the parent `.claude/` directory) for launching via Claude Code:

| Name | Command |
|------|---------|
| `Scanner` | `py -m scanner.runner` |
| `Tests` | `py -m pytest tests/ -v` |

---

## Test Coverage

253 tests across 7 test files:

| File | Tests |
|------|-------|
| `tests/test_arb_executor.py` | 29 |
| `tests/test_kalshi_client.py` | 75 |
| `tests/test_kalshi_trader.py` | 23 |
| `tests/test_market_matcher.py` | 37 |
| `tests/test_opportunity_finder.py` | 30 |
| `tests/test_poly_client.py` | 46 |
| `tests/test_poly_trader.py` | 13 |

---

## Configuration Reference (`scanner/config.py`)

| Constant | Default | Description |
|----------|---------|-------------|
| `MARKET_REFRESH_SECONDS` | `7200` | Market list refresh interval (2 hours) |
| `PRICE_POLL_SECONDS` | `2` | Price poll interval (2 seconds) |
| `SCAN_WINDOW_HOURS` | `72` | Only include markets closing within this window |
| `RESOLUTION_TIME_TOLERANCE_HOURS` | `1` | Max close-time difference for a valid match |
| `CRYPTO_MATCHING_ENABLED` | `False` | Enable/disable crypto market matching |
| `MIN_SPREAD_CENTS` | `0.8` | Minimum spread to report an opportunity |
| `EXEC_MAX_TRADE_USD` | `5.0` | Maximum combined spend per trade (both legs) |
| `EXEC_POLY_MIN_ORDER_USD` | `1.0` | Minimum Polymarket order size per leg |
| `EXEC_COOLDOWN_CYCLES` | `15` | Price cycles to wait between trades on same pair (~30s) |
| `EXEC_UNWIND_DELAY_SECONDS` | `2.0` | Delay before first Kalshi unwind attempt |
| `FETCH_WORKERS` | `20` | Parallel threads for CLOB price fetching |

---

## API Endpoints

| Platform | Endpoint |
|----------|---------|
| Kalshi market list | `GET https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000` |
| Kalshi market price | `GET https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}` |
| Kalshi orderbook | `GET https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook` |
| Kalshi order placement | `POST https://api.elections.kalshi.com/trade-api/v2/portfolio/orders` |
| Polymarket market list | `GET https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500` |
| Polymarket CLOB prices | `GET https://clob.polymarket.com/book?token_id={token_id}` |

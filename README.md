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
| `scanner/poly_client.py` | Fetches Polymarket markets via Gamma API; polls CLOB prices and full ask ladder per token |
| `scanner/market_matcher.py` | Matches Kalshi and Polymarket markets by sport, team, opponent, date, subtype, and map number |
| `scanner/opportunity_finder.py` | Detects arbitrage from matched pair prices; tiers opportunities by spread size |
| `scanner/match_validator.py` | Verifies sports matches are actually scheduled on Liquipedia before allowing trades (disabled by default) |
| `scanner/arb_executor.py` | Executes two-leg trades; handles position sizing, book-walk, cooldowns, and Kalshi unwind on Polymarket failure |
| `scanner/kalshi_trader.py` | Kalshi order placement via RSA-PS256 signed REST API |
| `scanner/poly_trader.py` | Polymarket order placement via `py-clob-client` (sig_type=2 proxy mode) |
| `scanner/models.py` | `NormalizedMarket`, `MatchedPair`, `Opportunity` dataclasses |
| `scanner/config.py` | All constants (timing, thresholds, env var names, API URLs) |
| `scanner/db.py` | SQLite persistence — `init_db`, `log_opportunity`, `log_trade` |

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

| Tier | Spread |
|------|--------|
| Ultra High | ≥ 8.0c |
| High | 5.0c – 8.0c |
| Mid | 4.0c – 5.0c |
| Low | 3.3c – 4.0c |

Opportunities below 3.3c (`MIN_SPREAD_CENTS`) are ignored.

---

## Match Validation

Before placing any trade on a sports market, the scanner can verify the match is actually scheduled on **[Liquipedia](https://liquipedia.net)** — the authoritative esports match database. This prevents losses from markets that open speculatively for matches that are never confirmed or get cancelled before play (e.g. the real-world BHE vs ShindeN loss that prompted this feature).

**Current status:** `MATCH_VALIDATION_ENABLED = False` — disabled because the Liquipedia API requires a paid subscription (~$50/month). The full implementation is preserved and can be re-enabled at any time.

**How it works (when enabled):**
1. Before evaluating opportunities for a sports pair, `match_validator.py` calls the **Liquipedia API v3** (`/api/v3/match`) with a 72-hour lookahead window
2. Both team names are fuzzy-matched against the returned team list (substring match + SequenceMatcher ratio ≥ 0.72)
3. If either team is missing → the pair is **skipped entirely** (no opportunity logged, no trade)
4. If Liquipedia is unavailable (no API key, timeout, etc.) → the pair is **allowed with a warning** (fail open)
5. Results are cached **per sport** for **30 minutes** — only one API call per sport per half-hour

**Supported sports (validated against Liquipedia):**

| Sport | Liquipedia Wiki |
|-------|----------------|
| CS2 | `counterstrike` |
| LOL | `leagueoflegends` |
| VALORANT | `valorant` |
| DOTA2 | `dota2` |
| RL | `rocketleague` |

Traditional sports (NBA, NFL, NHL, MLB, Soccer) pass through without validation — no equivalent Liquipedia match schedule.

| Config | Default | Description |
|--------|---------|-------------|
| `MATCH_VALIDATION_ENABLED` | `False` | Enable/disable the Liquipedia check (requires API key) |
| `SKIP_UNVERIFIED_MATCHES` | `True` | Skip unverified pairs (`False` = warn only, still trade) |

To enable, set `MATCH_VALIDATION_ENABLED = True` in `config.py` and add `LIQUIPEDIA_API_KEY` to your `.env`. Free key available at [api.liquipedia.net](https://api.liquipedia.net/).

---

## Paper Trading (Dry Run)

Run the scanner in **paper mode** to simulate what would happen with $20K virtual capital — no real orders are placed.

```bash
py -m scanner.runner --paper
```

**What it does:**
- Starts with a virtual wallet: **$10,000 Kalshi + $10,000 Polymarket**
- Uses the exact same opportunity detection, market validation, and position sizing as live mode
- Simulates instant full fills at the current ask price (no slippage)
- Tracks Kalshi 1.75% fees on every simulated trade
- Writes all simulated trades to **`scanner_paper.db`** (separate from the live `scanner.db`)
- Prints a full wallet report every ~3 minutes and on exit (Ctrl+C)

**Sample report:**
```
============================================================
  PAPER TRADING REPORT
============================================================
  Initial capital   :  $20,000.00
  Kalshi balance    :   $9,623.40
  Poly balance      :   $9,234.12
  Deployed          :   $1,142.48  (5.7% of capital)

  Trades simulated  : 34
  Gross profit      :      $62.34
  Kalshi fees (est) :       $9.87
  Net profit        :      $52.47
  Net ROI on deployed:       4.59%

  Best trade  : $6.72  — GLSIMP-2-IMP | 9.0c spread | 74 units
  Worst trade : $0.21  — BHESHIN-1    | 5.0c spread | 4 units
============================================================
```

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

# Liquipedia API (optional — only needed if MATCH_VALIDATION_ENABLED = True)
# Free key at https://api.liquipedia.net/
LIQUIPEDIA_API_KEY=<your_key>
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
    EXEC_MAX_UNITS_PER_MAP,                       # hard cap per map market (300)
)
```

If the resulting unit count is below Polymarket's $1 minimum order but additional depth exists at higher price levels, the executor **book-walks** the Polymarket ask ladder:

1. Collects shares level by level until the $1 minimum is met
2. Computes a **blended (weighted average) price** across all levels consumed
3. Re-checks whether the spread is still above `MIN_SPREAD_CENTS` at the blended price
4. If yes — executes at the blended price; if no — skips the trade

This prevents thin-depth markets from being silently skipped when just a few extra shares at the next level would make the trade viable.

- Max total spend per trade: `$50.00` (configurable via `EXEC_MAX_TRADE_USD` in `config.py`)
- Polymarket minimum order: `$1.00` per leg (`EXEC_POLY_MIN_ORDER_USD`)
- Hard cap per map market: `300` units (`EXEC_MAX_UNITS_PER_MAP`)

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
| **Pair cooldown** | After each successful trade, the pair is locked for 5 price cycles (~10 seconds). After a failed trade (unwind triggered), the cooldown is doubled (~20 seconds). |
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

# Paper trade mode (no real orders)
py -m scanner.runner --paper

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
| `scanner.db` | SQLite database — `opportunities` and `trades` tables (see below) |
| `scanner_paper.db` | Separate SQLite database used in `--paper` (dry-run) mode |

### SQLite Database (`scanner.db`)

**`opportunities` table** — every arb opportunity detected (whether traded or not):

| Column | Description |
|--------|-------------|
| `scanned_at` | UTC timestamp of detection |
| `kalshi_ticker` | Kalshi market ticker |
| `poly_token_id` | Polymarket token ID |
| `kalshi_title` / `poly_title` | Raw market question text |
| `strategy` | `A` (K-YES + P-NO) or `B` (K-NO + P-YES) |
| `kalshi_side` / `poly_side` | Which side was bought on each platform |
| `kalshi_cost_cents` / `poly_cost_cents` | Price at detection time |
| `spread_cents` | Guaranteed profit per share in cents |
| `tier` | Low / Mid / High / Ultra High |
| `kalshi_depth_contracts` | Contracts available at that Kalshi ask price |
| `poly_depth_shares` | Shares available at that Poly ask price |
| `tradeable_units` | `min(k_depth, p_depth)` — max fillable at this spread |
| `max_locked_profit_usd` | `tradeable_units × spread / 100` — total capturable profit |
| `hours_to_close` | Hours until earlier market closes |
| `kalshi_close_time` / `poly_close_time` | Market resolution timestamps |
| `executed` | `1` if a trade was attempted, `0` if scan-only |

**`trades` table** — every trade execution attempt:

| Column | Description |
|--------|-------------|
| `opportunity_id` | FK → `opportunities.id` |
| `traded_at` | UTC timestamp of execution |
| `kalshi_ticker` / `poly_token_id` | Market identifiers |
| `kalshi_side` / `poly_side` | Sides traded |
| `requested_units` | Units calculated before placing order |
| `kalshi_filled` / `poly_filled` | Actual contracts/shares filled |
| `kalshi_price_cents` / `poly_price_cents` | Prices at execution time (blended if book-walked) |
| `kalshi_cost_usd` / `poly_cost_usd` / `total_cost_usd` | USD spent per leg and combined |
| `locked_profit_usd` | Guaranteed gross profit before fees (spread × units / 100) |
| `kalshi_fee_usd` | Kalshi taker fee: 1.75% of face value (filled contracts × $1) |
| `net_profit_usd` | Real profit after fees: `locked_profit_usd − kalshi_fee_usd` |
| `kalshi_order_id` / `poly_order_id` | Platform order IDs |
| `status` | `filled` / `skipped` / `unwound` / `partial_stuck` / `error` |
| `reason` | Skip/fail reason code |
| `poly_balance_before` | Poly USDC balance before trade |

---

## Dev Server Config

The project includes `.claude/launch.json` (in the parent `.claude/` directory) for launching via Claude Code:

| Name | Command |
|------|---------|
| `Scanner` | `py -m scanner.runner` |
| `Paper Trade` | `py -m scanner.runner --paper` |
| `Tests` | `py -m pytest tests/ -v` |

---

## Test Coverage

325 tests across 9 test files:

| File | Tests |
|------|-------|
| `tests/test_arb_executor.py` | 33 |
| `tests/test_kalshi_client.py` | 75 |
| `tests/test_kalshi_trader.py` | 23 |
| `tests/test_market_matcher.py` | 37 |
| `tests/test_match_validator.py` | 45 |
| `tests/test_opportunity_finder.py` | 30 |
| `tests/test_paper_executor.py` | 23 |
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
| `MIN_SPREAD_CENTS` | `3.3` | Minimum spread to report an opportunity |
| `EXEC_MAX_TRADE_USD` | `50.0` | Maximum combined spend per trade (both legs) |
| `EXEC_MAX_UNITS_PER_MAP` | `300` | Hard cap on units per single map market |
| `EXEC_POLY_MIN_ORDER_USD` | `1.0` | Minimum Polymarket order size per leg |
| `EXEC_COOLDOWN_CYCLES` | `5` | Price cycles to wait between trades on same pair (~10s) |
| `EXEC_UNWIND_DELAY_SECONDS` | `2.0` | Delay before first Kalshi unwind attempt |
| `KALSHI_TAKER_FEE_RATE` | `0.0175` | Kalshi taker fee rate (1.75% of face value per fill) |
| `MATCH_VALIDATION_ENABLED` | `False` | Enable/disable Liquipedia match validation (requires API key, ~$50/mo) |
| `SKIP_UNVERIFIED_MATCHES` | `True` | Skip pair if not found on Liquipedia (`False` = warn only) |
| `FETCH_WORKERS` | `20` | Parallel threads for CLOB price fetching |

---

## API Endpoints

| Platform | Endpoint |
|----------|---------|
| Kalshi market list | `GET https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000` |
| Kalshi market price | `GET https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}` — `yes_ask`/`no_ask` may be null; orderbook is used as fallback |
| Kalshi orderbook | `GET https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook` — authoritative price source + depth |
| Kalshi order placement | `POST https://api.elections.kalshi.com/trade-api/v2/portfolio/orders` |
| Polymarket market list | `GET https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500` |
| Polymarket CLOB prices | `GET https://clob.polymarket.com/book?token_id={token_id}` — returns full ask ladder for book-walk |
| Liquipedia matches | `GET https://api.liquipedia.net/api/v3/match` — requires API key |

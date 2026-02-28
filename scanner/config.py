"""Constants and configuration for the BothMarkets cross-platform arb scanner."""

# --- Loop timing ---
MARKET_REFRESH_SECONDS = 7200   # 2 hours: how often to re-fetch market lists and re-match
PRICE_POLL_SECONDS = 2          # 2 seconds: how often to fetch live prices and check for arb

# --- Scan window ---
SCAN_WINDOW_HOURS = 72          # Only include markets closing within this window

# --- Market matching tolerance ---
RESOLUTION_TIME_TOLERANCE_HOURS = 1   # Max difference between Kalshi and Poly close times

# --- Crypto matching ---
# Disabled: Kalshi resolves via CF Benchmarks BRTI (60-sec multi-exchange average)
# while Polymarket resolves via Binance 1-min candle close. The two oracles can
# diverge at settlement, meaning a "covered" position (YES on one, NO on the other
# at the same threshold) is NOT risk-free. Additionally, Kalshi closes at 5pm ET
# while Polymarket closes at 12pm ET — a structural 5-hour gap that fails the 1-hour
# date tolerance. Enable only if you fully understand and accept oracle/timing risk.
CRYPTO_MATCHING_ENABLED = False

# --- Arbitrage thresholds ---
# Tiers account for cash transfer fees between platforms
PROFIT_TIERS = [
    ("Ultra High", 8.0, float("inf")),
    ("High",       5.0, 8.0),
    ("Mid",        4.0, 5.0),
    ("Low",        3.3, 4.0),
]
MIN_SPREAD_CENTS = 3.3          # Ignore anything below this
MIN_PRICE_CENTS = 5.0           # Skip legs priced below this (near-zero tokens can't meet Poly $1 min)

# --- Kalshi API ---
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PAGE_LIMIT = 1000        # Max markets per page (API max)
KALSHI_RATE_LIMIT_SLEEP = 0.06  # 60ms between pages → stays under 20 req/sec Basic tier

# --- Polymarket APIs ---
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
GAMMA_PAGE_LIMIT = 500          # Gamma API max per offset page

# --- HTTP ---
HTTP_TIMEOUT = 15.0             # Seconds for httpx requests
FETCH_WORKERS = 20              # Max parallel threads for CLOB price fetching

# --- Output files ---
LOG_FILE = "scanner.log"
OPPS_LOG_FILE = "opportunities.log"   # Filtered: matched pairs + arb opportunities only
OPPS_JSON_FILE = "opportunities.json" # NDJSON: one object per scan run
DB_FILE = "scanner.db"                # SQLite: opportunities + trades tables
DRY_RUN_DB_FILE = "scanner_paper.db"  # Separate DB used in --paper (dry-run) mode

# --- Fees ---
# Kalshi charges 1.75% of face value (contracts × $1) on taker fills.
KALSHI_TAKER_FEE_RATE: float = 0.0175
#
# Polymarket fee structure (per docs.polymarket.com/trading/fees):
#   - Esports (CS2, LOL, VALORANT, DOTA2, RL): 0% — fee-free
#   - Standard sports (NBA, NFL, NHL, MLB):     0% — fee-free
#   - NCAAB / Serie A (from Feb 18, 2026):      up to 0.44% at $0.50 price
#   - 5/15-min crypto mini-markets:             up to 1.56% at $0.50 price
# All markets currently scanned (esports + NBA/NFL/etc.) are fee-free on Polymarket.
# No POLY_TAKER_FEE_RATE constant needed for current market scope.
POLY_TAKER_FEE_RATE: float = 0.0   # effectively 0 for all esports and standard sports

# --- Match validation ---
# Before trading a sports market, verify the match appears on Liquipedia's
# upcoming schedule.  Prevents arb losses from cancelled / never-scheduled events.
MATCH_VALIDATION_ENABLED: bool = False  # Liquipedia API costs $50/mo — disabled for now, code kept as backup
SKIP_UNVERIFIED_MATCHES: bool = True    # True = skip pair | False = allow with warning only

# --- Execution layer ---
# Maximum total USD spend per trade (both legs combined).
EXEC_MAX_TRADE_USD: float = 50.0
# Hard cap on units per single map market (prevents over-investment on thin markets).
EXEC_MAX_UNITS_PER_MAP: int = 300
# Minimum per Polymarket leg in USD (Polymarket rejects orders below ~$1)
EXEC_POLY_MIN_ORDER_USD: float = 1.0
# Cycles to wait before re-executing on the same pair (1 cycle ≈ 2 seconds)
EXEC_COOLDOWN_CYCLES: int = 5           # ~10s cooldown between trades on same pair
# Seconds to wait before attempting to unwind a failed Kalshi leg
EXEC_UNWIND_DELAY_SECONDS: float = 2.0

# --- Environment variable names ---
ENV_LIQUIPEDIA_API_KEY = "LIQUIPEDIA_API_KEY"   # Free key: https://api.liquipedia.net/
ENV_KALSHI_API_KEY = "KALSHI_API_KEY"
ENV_KALSHI_API_SECRET = "KALSHI_API_SECRET"
ENV_POLY_PRIVATE_KEY = "POLY_PRIVATE_KEY"
ENV_POLY_API_KEY = "POLY_API_KEY"
ENV_POLY_API_SECRET = "POLY_API_SECRET"
ENV_POLY_API_PASSPHRASE = "POLY_API_PASSPHRASE"
ENV_POLY_FUNDER = "POLY_FUNDER"

# --- Market URL templates ---
KALSHI_MARKET_URL = "https://kalshi.com/markets/{ticker}"
POLY_MARKET_URL = "https://polymarket.com/event/{slug}"

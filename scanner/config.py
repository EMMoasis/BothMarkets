"""Constants and configuration for the BothMarkets cross-platform arb scanner."""

# --- Loop timing ---
MARKET_REFRESH_SECONDS = 7200   # 2 hours: how often to re-fetch market lists and re-match
PRICE_POLL_SECONDS = 2          # 2 seconds: how often to fetch live prices and check for arb

# --- Scan window ---
SCAN_WINDOW_HOURS = 72          # Only include markets closing within this window

# --- Market matching tolerance ---
RESOLUTION_TIME_TOLERANCE_HOURS = 1   # Max difference between Kalshi and Poly close times

# --- Arbitrage thresholds ---
# Tiers raised +0.5c vs raw spread to account for cash transfer fees between platforms
PROFIT_TIERS = [
    ("Ultra High", 5.5, float("inf")),
    ("High",       2.5, 5.5),
    ("Mid",        1.5, 2.5),
    ("Low",        0.8, 1.5),
]
MIN_SPREAD_CENTS = 0.8          # Ignore anything below this

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

# --- Environment variable names ---
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

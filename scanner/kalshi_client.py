"""Kalshi REST API connector — market list fetching, normalization, and live price polling.

Handles two market types:
  CRYPTO: "Will BTC be above $90,000 on Feb 21?" — asset + direction + threshold + date
  SPORTS: "Will M80 win the M80 vs. Voca CS2 match?" — team + opponent + sport + date
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from scanner.config import (
    FETCH_WORKERS,
    HTTP_TIMEOUT,
    KALSHI_BASE_URL,
    KALSHI_MARKET_URL,
    KALSHI_PAGE_LIMIT,
    KALSHI_RATE_LIMIT_SLEEP,
    MARKET_REFRESH_SECONDS,
    SCAN_WINDOW_HOURS,
)
from scanner.models import MarketType, NormalizedMarket, Platform

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Crypto: asset keyword map
# ---------------------------------------------------------------------------
_ASSET_MAP: dict[str, str] = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "xrp": "XRP", "ripple": "XRP",
    "solana": "SOL", "sol": "SOL",
    "dogecoin": "DOGE", "doge": "DOGE",
    "bnb": "BNB", "binance": "BNB",
    "cardano": "ADA", "ada": "ADA",
    "avalanche": "AVAX", "avax": "AVAX",
    "polygon": "MATIC", "matic": "MATIC",
    "litecoin": "LTC", "ltc": "LTC",
}

_ABOVE_WORDS = {"above", "over", "exceed", "exceeds", "higher", "more", "greater",
                "reach", "reaches", "hit", "hits", "surpass"}
_BELOW_WORDS = {"below", "under", "less", "lower", "beneath", "fall", "falls",
                "drop", "drops"}

# ---------------------------------------------------------------------------
# Sports: series ticker → sport code
# e.g. KXCS2GAME → CS2, KXNBAWIN → NBA, KXNHLWIN → NHL
# ---------------------------------------------------------------------------
_SPORT_SERIES: dict[str, str] = {
    "KXCS2GAME":  "CS2",
    "KXCS2MAP":   "CS2",
    "KXCS2":      "CS2",
    "KXNBAWIN":   "NBA",
    "KXNBA":      "NBA",
    "KXMLBWIN":   "MLB",
    "KXMLB":      "MLB",
    "KXNHLWIN":   "NHL",
    "KXNHL":      "NHL",
    "KXNFLWIN":   "NFL",
    "KXNFL":      "NFL",
    "KXSOCCER":   "SOCCER",
    "KXLOLGAME":  "LOL",
    "KXLOLMAP":   "LOL",
    "KXLOLWIN":   "LOL",
    "KXLOL":      "LOL",
    "KXVALORANTMAP": "VALORANT",
    "KXVALORANT": "VALORANT",
    "KXDOTA2GAME": "DOTA2",
    "KXDOTA2":    "DOTA2",
    "KXROCKETLEAGUE": "RL",
    "KXRL":       "RL",
}

# Maps series ticker → (series_slug) used to build the Kalshi market page URL.
# URL format: https://kalshi.com/markets/{series_lower}/{series_slug}/{event_lower}
# Series slugs come from the series title, lowercased with spaces→hyphens.
_SERIES_URL_SLUG: dict[str, str] = {
    "KXCS2GAME":     "counter-strike-2-game",
    "KXCS2MAP":      "counter-strike-2-map-winner",
    "KXCS2TOTALMAPS": "counter-strike-2-total-maps",
    "KXLOLGAME":     "league-of-legends-game",
    "KXLOLMAP":      "league-of-legends-map-winner",
    "KXVALORANTMAP": "valorant-map-winner",
    "KXVALORANTGAME": "valorant-game",
    "KXDOTA2GAME":   "dota-2-game",
    "KXDOTA2MAP":    "dota-2-map-winner",
    "KXNBAWIN":      "nba-game-winner",
    "KXNHLWIN":      "nhl-game-winner",
    "KXNFLWIN":      "nfl-game-winner",
    "KXMLBWIN":      "mlb-game-winner",
    "KXROCKETLEAGUE": "rocket-league-game",
}

# Series prefixes that represent individual map/game winner markets (not full series winner)
_MAP_SERIES_PREFIXES: set[str] = {
    "KXCS2MAP", "KXLOLMAP", "KXVALORANTMAP", "KXDOTA2MAP",
}


def _get_sport_subtype(series_ticker: str) -> str:
    """Return 'map' for per-map/game winner markets, 'series' for match/series winner markets."""
    s = series_ticker.upper()
    for prefix in _MAP_SERIES_PREFIXES:
        if s.startswith(prefix):
            return "map"
    return "series"


class KalshiClient:
    """
    Fetches and normalizes Kalshi binary markets.
    No authentication required for market reading (Basic tier).

    Returns NormalizedMarket for two types:
      - MarketType.CRYPTO: crypto price markets
      - MarketType.SPORTS: sports game-winner markets
    """

    def __init__(self) -> None:
        self._cached_markets: list[NormalizedMarket] | None = None
        self._cache_time: float = 0.0
        self._http = httpx.Client(
            timeout=HTTP_TIMEOUT,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    def get_all_markets(self, force_refresh: bool = False) -> list[NormalizedMarket]:
        """
        Return all open Kalshi markets closing within SCAN_WINDOW_HOURS.
        Includes both crypto price markets and sports game-winner markets.
        Uses a 2-hour cache. Set force_refresh=True to bypass cache.
        """
        now = time.monotonic()
        if not force_refresh and self._cached_markets is not None:
            age = now - self._cache_time
            if age < MARKET_REFRESH_SECONDS:
                log.debug("Kalshi: using cached %d markets (age %.0fs)", len(self._cached_markets), age)
                return self._cached_markets

        log.info("Kalshi: fetching market list...")
        raw = self._fetch_all_pages()
        normalized = self._normalize_batch(raw)
        filtered = self._filter_by_window(normalized)

        self._cached_markets = filtered
        self._cache_time = time.monotonic()

        crypto = [m for m in filtered if m.market_type == MarketType.CRYPTO]
        sports = [m for m in filtered if m.market_type == MarketType.SPORTS]
        log.info(
            "Kalshi: %d raw → %d normalized → %d in 72h window (%d crypto, %d sports)",
            len(raw), len(normalized), len(filtered), len(crypto), len(sports),
        )
        return filtered

    def fetch_live_prices(self, markets: list[NormalizedMarket]) -> dict[str, dict[str, float | None]]:
        """
        Fetch current yes/no ask prices for a list of Kalshi markets in parallel.

        Returns {ticker: {"yes_ask": float|None, "no_ask": float|None,
                           "yes_bid": float|None, "no_bid": float|None}}
        Prices are in cents (0-100).
        """
        if not markets:
            return {}

        tickers = [m.platform_id for m in markets]

        def fetch_one(ticker: str) -> tuple[str, dict[str, float | None]]:
            try:
                resp = self._http.get(f"{KALSHI_BASE_URL}/markets/{ticker}")
                resp.raise_for_status()
                data = resp.json().get("market", {})
                return ticker, {
                    "yes_ask": _to_cents(data.get("yes_ask")),
                    "no_ask":  _to_cents(data.get("no_ask")),
                    "yes_bid": _to_cents(data.get("yes_bid")),
                    "no_bid":  _to_cents(data.get("no_bid")),
                }
            except Exception:
                log.debug("Kalshi: price fetch failed for %s", ticker, exc_info=True)
                return ticker, {"yes_ask": None, "no_ask": None, "yes_bid": None, "no_bid": None}

        results: dict[str, dict[str, float | None]] = {}
        with ThreadPoolExecutor(max_workers=min(len(tickers), FETCH_WORKERS)) as pool:
            for ticker, data in pool.map(fetch_one, tickers):
                results[ticker] = data

        return results

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _fetch_all_pages(self) -> list[dict[str, Any]]:
        """
        Paginate GET /markets until no more cursor.
        Respects Basic-tier rate limit (20 req/sec) with 60ms sleep between pages.
        """
        all_markets: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            params: dict[str, Any] = {"status": "open", "limit": KALSHI_PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor

            resp = self._http.get(f"{KALSHI_BASE_URL}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()

            page = data.get("markets", [])
            all_markets.extend(page)

            cursor = data.get("cursor") or None
            if not cursor or len(page) < KALSHI_PAGE_LIMIT:
                break

            time.sleep(KALSHI_RATE_LIMIT_SLEEP)

        return all_markets

    def _normalize_batch(self, raw: list[dict[str, Any]]) -> list[NormalizedMarket]:
        result: list[NormalizedMarket] = []
        for item in raw:
            try:
                market = _normalize_one(item)
                if market is not None:
                    result.append(market)
            except Exception:
                log.debug("Kalshi: normalization failed for %s", item.get("ticker", "?"), exc_info=True)
        return result

    def _filter_by_window(self, markets: list[NormalizedMarket]) -> list[NormalizedMarket]:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=SCAN_WINDOW_HOURS)
        return [m for m in markets if now < m.resolution_dt <= cutoff]


# ------------------------------------------------------------------
# Module-level helpers (also used by poly_client for shared parsing)
# ------------------------------------------------------------------

def _normalize_one(raw: dict[str, Any]) -> NormalizedMarket | None:
    """
    Convert a single Kalshi API market dict to NormalizedMarket.
    Returns None if the market is unparseable or not a supported type.

    Tries sports parsing first (faster series-ticker check),
    then falls back to crypto parsing.
    """
    ticker = raw.get("ticker", "").strip()
    title = (raw.get("title") or "").strip()
    expiry_str = (raw.get("expected_expiration_time") or "").strip()

    if not ticker or not title or not expiry_str:
        return None

    resolution_dt = parse_iso(expiry_str)
    if resolution_dt is None:
        return None

    # Try sports first (series_ticker check is O(1))
    series_ticker = (raw.get("series_ticker") or "").upper().strip()
    sport = _get_sport(series_ticker, ticker)
    if sport:
        return _normalize_sports(raw, ticker, title, resolution_dt, sport)

    # Fall back to crypto
    return _normalize_crypto(raw, ticker, title, resolution_dt)


def _kalshi_market_url(series_ticker: str, event_ticker: str) -> str:
    """
    Build the correct Kalshi market page URL.
    Format: https://kalshi.com/markets/{series_lower}/{series_slug}/{event_lower}
    Falls back to the old /markets/{ticker} format if series slug is unknown.
    """
    slug = _SERIES_URL_SLUG.get(series_ticker.upper())
    if slug and event_ticker:
        return f"https://kalshi.com/markets/{series_ticker.lower()}/{slug}/{event_ticker.lower()}"
    # fallback: use event_ticker as path (better than nothing)
    return f"https://kalshi.com/markets/{(event_ticker or series_ticker).lower()}"


def _get_sport(series_ticker: str, ticker: str) -> str | None:
    """Return sport code if this market belongs to a known sports series, else None."""
    # Exact match on series_ticker
    if series_ticker in _SPORT_SERIES:
        return _SPORT_SERIES[series_ticker]
    # Prefix match for variants (e.g. KXCS2GAME-26FEB...)
    for prefix, sport in _SPORT_SERIES.items():
        if series_ticker.startswith(prefix):
            return sport
    # Fallback: check ticker itself
    ticker_upper = ticker.upper()
    for prefix, sport in _SPORT_SERIES.items():
        if ticker_upper.startswith(prefix):
            return sport
    return None


def _normalize_sports(
    raw: dict[str, Any],
    ticker: str,
    title: str,
    resolution_dt: datetime,
    sport: str,
) -> NormalizedMarket | None:
    """
    Normalize a Kalshi sports game-winner market.

    Expected title format: "Will [TEAM] win the [TEAM A] vs. [TEAM B] [SPORT] match?"
    The team this market is for (YES = this team wins) is in yes_sub_title.
    The event_ticker groups both team markets of the same match.
    """
    # yes_sub_title = the team name this YES market is for
    team_raw = (raw.get("yes_sub_title") or "").strip()
    event_ticker = (raw.get("event_ticker") or "").strip()
    series_ticker = (raw.get("series_ticker") or "").strip().upper()
    # Derive series_ticker from the market ticker if not provided
    if not series_ticker:
        for prefix in _SERIES_URL_SLUG:
            if ticker.upper().startswith(prefix):
                series_ticker = prefix
                break

    if not team_raw:
        # Fall back: try to extract team from title
        team_raw = _extract_winner_team_from_title(title)
    if not team_raw:
        return None

    # Extract both teams from title to get opponent
    team_a, team_b = _extract_both_teams(title)
    if team_a and team_b:
        # Figure out which is the opponent
        team_norm = normalize_team_name(team_raw)
        team_a_norm = normalize_team_name(team_a)
        team_b_norm = normalize_team_name(team_b)
        if team_norm == team_a_norm:
            opponent_raw = team_b
        elif team_norm == team_b_norm:
            opponent_raw = team_a
        else:
            # Our team didn't match either — use whichever isn't our team by string
            opponent_raw = team_b if team_raw.lower() in team_a.lower() else team_a
    else:
        # Can't extract opponent — skip (needed for matching)
        return None

    team_norm = normalize_team_name(team_raw)
    opponent_norm = normalize_team_name(opponent_raw)

    # Build the correct Kalshi page URL using series + event ticker
    platform_url = _kalshi_market_url(series_ticker, event_ticker) if series_ticker and event_ticker \
        else KALSHI_MARKET_URL.format(ticker=ticker)

    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_id=ticker,
        platform_url=platform_url,
        raw_question=title,
        market_type=MarketType.SPORTS,
        asset=sport,
        direction="WIN",
        threshold=0.0,
        team=team_norm,
        opponent=opponent_norm,
        sport=sport,
        sport_subtype=_get_sport_subtype(series_ticker),
        event_id=event_ticker,
        resolution_dt=resolution_dt,
        yes_ask_cents=_to_cents(raw.get("yes_ask")),
        no_ask_cents=_to_cents(raw.get("no_ask")),
        yes_bid_cents=_to_cents(raw.get("yes_bid")),
        no_bid_cents=_to_cents(raw.get("no_bid")),
        liquidity_usd=float(raw.get("liquidity") or 0),
        raw_data=raw,
    )


def _normalize_crypto(
    raw: dict[str, Any],
    ticker: str,
    title: str,
    resolution_dt: datetime,
) -> NormalizedMarket | None:
    """Normalize a Kalshi crypto price market."""
    asset = extract_asset(title)
    direction = extract_direction(title)
    threshold = extract_dollar_amount(title)

    if asset is None or direction is None or threshold is None:
        return None

    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_id=ticker,
        platform_url=KALSHI_MARKET_URL.format(ticker=ticker),
        raw_question=title,
        market_type=MarketType.CRYPTO,
        asset=asset,
        direction=direction,
        threshold=threshold,
        resolution_dt=resolution_dt,
        yes_ask_cents=_to_cents(raw.get("yes_ask")),
        no_ask_cents=_to_cents(raw.get("no_ask")),
        yes_bid_cents=_to_cents(raw.get("yes_bid")),
        no_bid_cents=_to_cents(raw.get("no_bid")),
        liquidity_usd=float(raw.get("liquidity") or 0),
        raw_data=raw,
    )


# ------------------------------------------------------------------
# Sports parsing helpers
# ------------------------------------------------------------------

def _extract_both_teams(title: str) -> tuple[str | None, str | None]:
    """
    Extract Team A and Team B from a title like:
      "Will M80 win the M80 vs. Voca CS2 match?"
      "Will Fnatic win the Fnatic vs. Team Vitality CS2 match?"
      "Will Team A win the Team A vs. Team B game?"

    Returns (team_a, team_b) or (None, None) if not parseable.
    """
    # Pattern: "the <TEAM A> vs[.] <TEAM B>" optionally followed by sport/match words
    # We need to handle team names with spaces (e.g. "Team Vitality", "Cloud9")
    patterns = [
        # "the X vs. Y CS2 match" or "the X vs. Y match"
        r'the\s+(.+?)\s+vs\.?\s+(.+?)\s+(?:cs2|nba|nfl|nhl|mlb|lol|valorant|dota|rocket\s*league|soccer|game|match|series)',
        # "the X vs. Y" at end
        r'the\s+(.+?)\s+vs\.?\s+(.+?)(?:\s*\?|$)',
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            a = m.group(1).strip()
            b = m.group(2).strip()
            if a and b and len(a) >= 1 and len(b) >= 1:
                return a, b
    return None, None


def _extract_winner_team_from_title(title: str) -> str | None:
    """
    Extract the winning team from "Will <TEAM> win the ..." pattern.
    Used as fallback when yes_sub_title is missing.
    """
    m = re.match(r'Will\s+(.+?)\s+win\s+', title, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def normalize_team_name(name: str) -> str:
    """
    Normalize a team name for cross-platform matching.
    - Lowercase
    - Remove common prefixes/suffixes: "team", "esports", "gaming", "fc", "sc"
    - Strip punctuation and extra spaces
    - Strip trailing numbers (e.g. "Cloud9 2" → "cloud9")

    This allows "M80" to match "M80", "Team Vitality" to match "vitality", etc.
    """
    s = name.lower().strip()
    # Remove punctuation except alphanumeric, spaces, dots
    s = re.sub(r"[^\w\s.]", "", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    # Remove common wrapper words that don't distinguish teams
    _STRIP_WORDS = {"team", "esports", "gaming", "fc", "sc", "g2", "the"}
    words = s.split()
    # Only strip if removing leaves something meaningful (≥1 word)
    if len(words) > 1:
        words = [w for w in words if w not in _STRIP_WORDS]
    s = " ".join(words).strip()
    # Remove trailing numbers after space (e.g. "Cloud9 2" → "cloud9")
    s = re.sub(r"\s+\d+$", "", s).strip()
    return s


# ------------------------------------------------------------------
# Crypto parsing helpers (also imported by poly_client)
# ------------------------------------------------------------------

def parse_iso(s: str) -> datetime | None:
    """Parse ISO 8601 UTC string to datetime. Returns None on failure."""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    # Try fromisoformat as fallback (handles microseconds)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def extract_asset(text: str) -> str | None:
    """Extract normalized asset ticker from market question text. Returns None if not found."""
    t = text.lower()
    for keyword, ticker in _ASSET_MAP.items():
        if keyword in t:
            return ticker
    return None


def extract_direction(text: str) -> str | None:
    """Extract 'ABOVE' or 'BELOW' direction from market question text. Returns None if not found."""
    t = text.lower()
    words = set(re.findall(r'\b\w+\b', t))
    if words & _ABOVE_WORDS:
        return "ABOVE"
    if words & _BELOW_WORDS:
        return "BELOW"
    return None


def extract_dollar_amount(text: str) -> float | None:
    """
    Extract the first dollar amount from text and return as base float.
    Handles: $90,000  $90k  $90K  $1.5M  $1.5m  $90000
    Returns None if no amount found.
    """
    clean = text.replace(",", "")
    pattern = r'\$\s*([\d]+(?:\.\d+)?)\s*([kKmMbB]?)'
    match = re.search(pattern, clean)
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2).lower()
    multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    return value * multipliers.get(suffix, 1)


def _to_cents(value: Any) -> float | None:
    """Convert a Kalshi price value (integer cents 0-100) to float cents. Returns None if missing."""
    if value is None:
        return None
    try:
        f = float(value)
        return f if 0.0 <= f <= 100.0 else None
    except (TypeError, ValueError):
        return None

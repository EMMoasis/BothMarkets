"""Polymarket connector — Gamma API for market discovery, CLOB for live prices.

Handles two market types:
  CRYPTO: "Bitcoin above $90k?" — asset + direction + threshold + date
  SPORTS: "Counter-Strike: NAVI Junior vs KUUSAMO.gg (BO3)" — split into per-team markets
          Each sports market becomes TWO NormalizedMarket objects (one per team).
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from scanner.config import (
    CLOB_API_URL,
    FETCH_WORKERS,
    GAMMA_API_URL,
    GAMMA_PAGE_LIMIT,
    HTTP_TIMEOUT,
    MARKET_REFRESH_SECONDS,
    POLY_MARKET_URL,
    SCAN_WINDOW_HOURS,
)
from scanner.kalshi_client import (
    _extract_map_number,
    extract_asset,
    extract_direction,
    extract_dollar_amount,
    normalize_team_name,
    parse_iso,
)
from scanner.models import MarketType, NormalizedMarket, Platform

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sports market detection
# ---------------------------------------------------------------------------
# Polymarket uses sportsMarketType field ("moneyline" = game winner)
_SPORTS_MARKET_TYPES = {"moneyline"}

# Category/tag words that indicate sports markets when sportsMarketType is absent
_SPORTS_CATEGORY_WORDS = {
    # Esports
    "cs2", "counter-strike", "esports", "valorant", "lol", "league of legends",
    "dota", "rocket league",
    # US Sports
    "nba", "nfl", "nhl", "mlb", "wnba",
    "basketball", "hockey", "baseball",
    "ncaa", "ncaab", "ncaaf", "college basketball", "college football",
    # Global Football / Soccer
    "soccer", "football",
    # Cricket
    "cricket", "ipl", "t20", "odi", "big bash", "bbl", "psl",
    # Tennis
    "tennis", "atp", "wta", "wimbledon", "grand slam",
    # Golf
    "golf", "pga", "pga tour", "masters", "liv golf", "ryder cup", "open championship",
    # MMA / Combat Sports
    "ufc", "mma", "mixed martial arts", "boxing",
    # Rugby
    "rugby", "nrl", "super rugby", "rugby league", "rugby union", "six nations",
    # Formula 1
    "formula 1", "formula one", "f1", "grand prix", "formula1",
    # Other
    "cfl", "afl", "australian football", "table tennis",
}

# Map sport category keyword → sport code
# Longer/more-specific keywords must come first so the sort-by-length in
# _detect_sport_from_text matches them before shorter substrings.
_POLY_SPORT_MAP: dict[str, str] = {
    # Esports
    "counter-strike": "CS2",
    "counter strike": "CS2",
    "cs2": "CS2",
    "league of legends": "LOL",
    "lol": "LOL",
    "rocket league": "RL",
    "valorant": "VALORANT",
    "dota": "DOTA2",
    # US Sports
    "nba": "NBA",
    "wnba": "WNBA",
    "nfl": "NFL",
    "nhl": "NHL",
    "mlb": "MLB",
    "basketball": "NBA",
    "hockey": "NHL",
    "baseball": "MLB",
    # College Sports
    "ncaa basketball": "NCAAB",
    "college basketball": "NCAAB",
    "march madness": "NCAAB",
    "ncaab": "NCAAB",
    "ncaa football": "NCAAF",
    "college football": "NCAAF",
    "ncaaf": "NCAAF",
    "cfb": "NCAAF",
    "ncaa": "NCAAB",            # generic fallback → basketball (most common)
    # Soccer / Football
    "soccer": "SOCCER",
    "football": "SOCCER",
    "premier league": "SOCCER",
    "champions league": "SOCCER",
    "mls": "SOCCER",
    "la liga": "SOCCER",
    "bundesliga": "SOCCER",
    "serie a": "SOCCER",
    "ligue 1": "SOCCER",
    # Cricket
    "big bash": "CRICKET",
    "indian premier league": "CRICKET",
    "cricket world cup": "CRICKET",
    "ipl": "CRICKET",
    "bbl": "CRICKET",
    "psl": "CRICKET",
    "t20": "CRICKET",
    "odi": "CRICKET",
    "cricket": "CRICKET",
    # Tennis
    "australian open": "TENNIS",
    "french open": "TENNIS",
    "roland garros": "TENNIS",
    "us open tennis": "TENNIS",
    "wimbledon": "TENNIS",
    "grand slam": "TENNIS",
    "atp": "TENNIS",
    "wta": "TENNIS",
    "tennis": "TENNIS",
    # Golf
    "pga tour": "GOLF",
    "liv golf": "GOLF",
    "ryder cup": "GOLF",
    "open championship": "GOLF",
    "masters": "GOLF",
    "pga": "GOLF",
    "golf": "GOLF",
    # MMA / Combat Sports
    "mixed martial arts": "MMA",
    "ufc": "MMA",
    "mma": "MMA",
    "boxing": "BOXING",
    # Rugby
    "rugby league": "RUGBY",
    "rugby union": "RUGBY",
    "six nations": "RUGBY",
    "super rugby": "RUGBY",
    "nrl": "RUGBY",
    "rugby": "RUGBY",
    # Formula 1
    "formula one": "F1",
    "formula 1": "F1",
    "formula1": "F1",
    "grand prix": "F1",
    "f1": "F1",
    # Other
    "canadian football": "CFL",
    "cfl": "CFL",
    "australian football": "AFL",
    "aussie rules": "AFL",
    "afl": "AFL",
    "table tennis": "TABLE_TENNIS",
    "lacrosse": "LACROSSE",
}


class PolyClient:
    """
    Fetches and normalizes Polymarket binary markets.

    Discovery: Gamma API (gamma-api.polymarket.com)
    Live prices: CLOB REST API (clob.polymarket.com/book?token_id=<id>)

    For SPORTS markets (sportsMarketType=moneyline):
      - Each Gamma market with N outcome teams → produces N NormalizedMarket objects
      - Each NormalizedMarket represents "team X wins this match"
      - yes_token_id = the token for that team's win
      - no_token_id = the OTHER team's win token (used to compute opponent price)

    No authentication needed for read operations.
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
        Return all Polymarket binary markets closing within SCAN_WINDOW_HOURS.

        Step 1: Fetch market list from Gamma API (discovery, cached 2h).
        Step 2: Filter to markets closing within window.
        Step 3: Fetch live CLOB prices in parallel for filtered markets.
        Step 4: Normalize and return NormalizedMarket list (sports markets split per-team).
        """
        now = time.monotonic()
        if not force_refresh and self._cached_markets is not None:
            age = now - self._cache_time
            if age < MARKET_REFRESH_SECONDS:
                log.debug("Polymarket: using cached %d markets (age %.0fs)",
                          len(self._cached_markets), age)
                return self._cached_markets

        log.info("Polymarket: fetching market list from Gamma API...")
        raw_gamma = self._fetch_gamma_markets()
        log.info("Polymarket: Gamma returned %d raw markets", len(raw_gamma))

        now_dt = datetime.now(timezone.utc)
        cutoff_dt = now_dt + timedelta(hours=SCAN_WINDOW_HOURS)
        candidates = [m for m in raw_gamma if _gamma_in_window(m, now_dt, cutoff_dt)]
        log.info("Polymarket: %d markets in 72h window", len(candidates))

        enriched = self._enrich_with_clob_prices(candidates)

        # Normalize — sports markets produce multiple NormalizedMarket objects
        normalized: list[NormalizedMarket] = []
        for gm in enriched:
            markets = _normalize_gamma_market(gm)
            normalized.extend(markets)

        self._cached_markets = normalized
        self._cache_time = time.monotonic()

        crypto = [m for m in normalized if m.market_type == MarketType.CRYPTO]
        sports = [m for m in normalized if m.market_type == MarketType.SPORTS]
        log.info(
            "Polymarket: normalized %d markets (%d crypto, %d sports team-entries)",
            len(normalized), len(crypto), len(sports),
        )
        return normalized

    def fetch_clob_prices(self, markets: list[NormalizedMarket]) -> dict[str, dict[str, float | None]]:
        """
        Fetch current YES and NO ask prices for a list of Polymarket markets in parallel.

        Returns {platform_id: {yes_ask, no_ask, yes_bid, no_bid}} (prices in cents).
        For sports: yes_token_id = this team wins, no_token_id = opponent wins.
        """
        if not markets:
            return {}

        results: dict[str, dict[str, float | None]] = {}

        def fetch_one(market: NormalizedMarket) -> tuple[str, dict]:
            yes_ask = no_ask = yes_bid = no_bid = None
            yes_ask_depth = no_ask_depth = None
            yes_ask_levels: list[tuple[float, float]] = []
            no_ask_levels:  list[tuple[float, float]] = []

            if market.yes_token_id:
                yes_ask, yes_bid, yes_ask_depth, yes_ask_levels = _fetch_book(self._http, market.yes_token_id)

            if market.no_token_id:
                no_ask, no_bid, no_ask_depth, no_ask_levels = _fetch_book(self._http, market.no_token_id)

            return market.platform_id, {
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "yes_bid": yes_bid,
                "no_bid": no_bid,
                "yes_ask_depth": yes_ask_depth,
                "no_ask_depth": no_ask_depth,
                "yes_ask_levels": yes_ask_levels,
                "no_ask_levels":  no_ask_levels,
            }

        with ThreadPoolExecutor(max_workers=min(len(markets), FETCH_WORKERS)) as pool:
            futures = {pool.submit(fetch_one, m): m for m in markets}
            for future in as_completed(futures):
                try:
                    cid, data = future.result()
                    results[cid] = data
                except Exception:
                    m = futures[future]
                    log.debug("Poly CLOB fetch failed for %s", m.platform_id, exc_info=True)
                    results[m.platform_id] = {
                        "yes_ask": None, "no_ask": None,
                        "yes_bid": None, "no_bid": None,
                    }

        return results

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _fetch_gamma_markets(self) -> list[dict[str, Any]]:
        """Paginate GET /markets from Gamma API using offset-based pagination."""
        all_markets: list[dict[str, Any]] = []
        offset = 0

        while True:
            params = {
                "active": "true",
                "closed": "false",
                "limit": GAMMA_PAGE_LIMIT,
                "offset": offset,
            }
            resp = self._http.get(f"{GAMMA_API_URL}/markets", params=params)
            resp.raise_for_status()
            page = resp.json()
            if not page:
                break
            all_markets.extend(page)
            if len(page) < GAMMA_PAGE_LIMIT:
                break
            offset += GAMMA_PAGE_LIMIT

        return all_markets

    def _enrich_with_clob_prices(
        self, gamma_markets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Fetch live CLOB prices for all tokens in parallel.
        For binary markets: fetches YES and NO tokens.
        For sports markets: fetches all team tokens.
        Injects _clob_prices: {token_id: (ask_cents, bid_cents)} into each dict.
        """
        # Collect all unique token IDs across all markets
        token_jobs: list[tuple[dict[str, Any], list[str]]] = []
        for gm in gamma_markets:
            token_ids = _extract_all_token_ids(gm)
            if token_ids:
                token_jobs.append((gm, token_ids))

        # Flatten unique token IDs
        all_tokens: set[str] = set()
        for _, ids in token_jobs:
            all_tokens.update(ids)

        # Fetch all in parallel
        # Values: (ask_cents, bid_cents, ask_depth, ask_levels)
        _BookEntry = tuple[float | None, float | None, float | None, list]
        token_prices: dict[str, _BookEntry] = {}

        def fetch_token(tid: str) -> tuple[str, _BookEntry]:
            return tid, _fetch_book(self._http, tid)

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            futures = {pool.submit(fetch_token, tid): tid for tid in all_tokens}
            for future in as_completed(futures):
                try:
                    tid, prices = future.result()
                    token_prices[tid] = prices
                except Exception:
                    tid = futures[future]
                    token_prices[tid] = (None, None, None, [])

        # Inject into each market dict
        enriched_list: list[dict[str, Any]] = []
        for gm in gamma_markets:
            enriched = dict(gm)
            token_ids = _extract_all_token_ids(gm)
            clob_prices: dict[str, _BookEntry] = {}
            for tid in token_ids:
                clob_prices[tid] = token_prices.get(tid, (None, None, None, []))
            enriched["_clob_prices"] = clob_prices
            enriched_list.append(enriched)

        return enriched_list


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _normalize_gamma_market(gm: dict[str, Any]) -> list[NormalizedMarket]:
    """
    Convert an enriched Gamma market dict to one or more NormalizedMarket objects.

    - Sports (moneyline): returns one NormalizedMarket per team (usually 2)
    - Crypto/binary: returns one NormalizedMarket (YES/NO)
    - Unparseable: returns empty list
    """
    if gm.get("closed", False) or not gm.get("active", True):
        return []

    condition_id = (gm.get("conditionId") or "").strip()
    question = (gm.get("question") or "").strip()
    end_date_str = (gm.get("endDate") or gm.get("endDateIso") or "").strip()

    # Prefer the parent EVENT slug (e.g. "lol-t1-dk-2026-02-22") over the market slug
    # (e.g. "lol-t1-dk-2026-02-22-game1") — the event slug is the working Polymarket URL.
    events = gm.get("events") or []
    event_slug = (events[0].get("slug") or "") if events else ""
    slug = event_slug or (gm.get("slug") or "").strip()

    if not condition_id or not question or not end_date_str:
        return []

    resolution_dt = parse_iso(end_date_str)
    if resolution_dt is None:
        return []

    platform_url = POLY_MARKET_URL.format(slug=slug) if slug else f"https://polymarket.com/event/{condition_id}"

    # Detect sports market
    # sportsMarketType values:
    #   "moneyline"       = full series/match winner  → sport_subtype "series"
    #   "child_moneyline" = individual map/game winner → sport_subtype "map"
    sports_type = (gm.get("sportsMarketType") or "").lower().strip()
    if sports_type in _SPORTS_MARKET_TYPES:
        return _normalize_sports_market(gm, condition_id, question, resolution_dt, platform_url, sports_type)

    # Check category/tags/series-slug for sports fallback
    sport_code = _detect_sport_from_question(question)
    if sport_code is None:
        # Try category field
        category = (gm.get("category") or gm.get("categories") or "").lower()
        sport_code = _detect_sport_from_text(category)
    if sport_code is None:
        # Try series slug (works for "Mavericks vs. Hornets" → seriesSlug="nba-2026")
        sport_code = _detect_sport_from_series_slug(_extract_series_slug(gm))

    if sport_code:
        # Has multiple non-YES/NO outcomes? Treat as sports moneyline
        outcomes = _parse_json_field(gm.get("outcomes")) or []
        if len(outcomes) >= 2 and not _is_yes_no_market(outcomes):
            return _normalize_sports_market(gm, condition_id, question, resolution_dt, platform_url, sports_type)

    # Crypto/binary market
    result = _normalize_crypto_market(gm, condition_id, question, resolution_dt, platform_url)
    if result:
        return [result]
    return []


def _normalize_yes_no_sports_market(
    gm: dict[str, Any],
    condition_id: str,
    question: str,
    resolution_dt: datetime,
    platform_url: str,
    token_ids: list,
    clob_prices: dict,
) -> list[NormalizedMarket]:
    """
    Handle YES/NO sports moneylines like "Will Austin FC win on 2026-03-01?"

    These single-team markets are common in soccer (MLS, Premier League, etc.)
    Each game typically has 3 separate binary questions: home-win, away-win, draw.
    We extract the winner team from the question text to allow Kalshi matching.

    Draw markets ("Will X vs. Y end in a draw?") are skipped — no Kalshi equivalent.
    """
    import re as _re
    # Skip draw/tie markets
    q_lower = question.lower()
    if any(kw in q_lower for kw in ("draw", "tie", "end in a")):
        return []

    # Extract winner team: "Will <TEAM> win..." or "<TEAM> wins..."
    m = _re.match(r'will\s+(.+?)\s+win\b', question, _re.IGNORECASE)
    if not m:
        return []
    team_raw = m.group(1).strip()
    if not team_raw:
        return []

    team_norm = normalize_team_name(team_raw)
    if not team_norm:
        return []

    # Detect sport
    sport_code = _detect_sport_from_question(question)
    if sport_code is None:
        category = (gm.get("category") or gm.get("categories") or "").lower()
        sport_code = _detect_sport_from_text(category)
    if sport_code is None:
        sport_code = _detect_sport_from_series_slug(_extract_series_slug(gm))
    if sport_code is None:
        sport_code = "SPORTS"

    # Token setup: YES=team wins, NO=team does not win
    yes_id = str(token_ids[0]) if token_ids else None
    no_id  = str(token_ids[1]) if len(token_ids) > 1 else None
    yes_ask, yes_bid, yes_ask_depth, yes_ask_levels = clob_prices.get(yes_id, (None, None, None, [])) if yes_id else (None, None, None, [])
    no_ask, no_bid, no_ask_depth, no_ask_levels     = clob_prices.get(no_id,  (None, None, None, [])) if no_id  else (None, None, None, [])

    synthetic_id = f"{condition_id}_{team_norm}"

    return [NormalizedMarket(
        platform=Platform.POLYMARKET,
        platform_id=synthetic_id,
        platform_url=platform_url,
        raw_question=question,
        market_type=MarketType.SPORTS,
        asset=sport_code,
        direction="WIN",
        threshold=0.0,
        team=team_norm,
        opponent=None,        # Opponent unknown from single-team binary market
        sport=sport_code,
        sport_subtype="series",
        event_id=condition_id,
        map_number=None,
        resolution_dt=resolution_dt,
        yes_ask_cents=yes_ask,
        no_ask_cents=no_ask,
        yes_bid_cents=yes_bid,
        no_bid_cents=no_bid,
        yes_ask_depth=yes_ask_depth,
        no_ask_depth=no_ask_depth,
        yes_ask_levels=yes_ask_levels,
        no_ask_levels=no_ask_levels,
        yes_token_id=yes_id,
        no_token_id=no_id,
        liquidity_usd=float(gm.get("liquidity") or 0),
        volume_usd=float(gm.get("volume") or 0),
        raw_data=gm,
    )]


def _normalize_sports_market(
    gm: dict[str, Any],
    condition_id: str,
    question: str,
    resolution_dt: datetime,
    platform_url: str,
    sports_type: str = "moneyline",
) -> list[NormalizedMarket]:
    """
    Normalize a Polymarket sports moneyline market into per-team NormalizedMarket objects.

    Each team gets:
      - platform_id: "{condition_id}_{team_norm}" (unique per team)
      - yes_token_id: token for THIS team winning
      - no_token_id: token for OPPONENT winning (for arb computation)
      - yes_ask_cents: ask price for this team to win
      - no_ask_cents: ask price for opponent to win (= price of being wrong)
    """
    outcomes = _parse_json_field(gm.get("outcomes")) or []
    token_ids = _parse_json_field(gm.get("clobTokenIds")) or []
    clob_prices: dict[str, tuple[float | None, float | None]] = gm.get("_clob_prices", {})

    if len(outcomes) < 2 or len(token_ids) < len(outcomes):
        return []

    # If outcomes are YES/NO this is a single-team binary market ("Will X win on DD-MM?").
    # Common for soccer leagues (MLS, Premier League, etc.) where each game has three
    # separate markets: home-win (YES/NO), away-win (YES/NO), draw (YES/NO).
    # We normalise these by extracting the winner team from the question text so they
    # can match Kalshi's "Will X win the X vs. Y match?" markets.
    if _is_yes_no_market(outcomes):
        return _normalize_yes_no_sports_market(gm, condition_id, question, resolution_dt, platform_url, token_ids, clob_prices)

    # Detect sport — cascade through all available signals
    sport_code = _detect_sport_from_question(question)
    if sport_code is None:
        category = (gm.get("category") or gm.get("categories") or "").lower()
        sport_code = _detect_sport_from_text(category)
    if sport_code is None:
        # Series slug is the most reliable source when question/category have no keyword.
        # e.g. "Mavericks vs. Hornets" has no "nba" keyword, but seriesSlug="nba-2026".
        sport_code = _detect_sport_from_series_slug(_extract_series_slug(gm))
    if sport_code is None:
        sport_code = "SPORTS"  # Generic fallback

    slug = (gm.get("slug") or "").strip()

    results: list[NormalizedMarket] = []
    for i, team_raw in enumerate(outcomes):
        team_raw = str(team_raw).strip()
        if not team_raw or team_raw.lower() in ("draw", "tie", "no contest"):
            continue

        team_token_id = str(token_ids[i]) if i < len(token_ids) else None
        if not team_token_id:
            continue

        # Opponent = the other team (for 2-team markets; skip for 3+ outcomes)
        if len(outcomes) == 2:
            opp_idx = 1 - i
            opp_raw = str(outcomes[opp_idx]).strip()
            opp_token_id = str(token_ids[opp_idx]) if opp_idx < len(token_ids) else None
        else:
            # For 3+ outcomes we can't trivially infer opponent
            continue

        team_norm = normalize_team_name(team_raw)
        opp_norm = normalize_team_name(opp_raw)

        # Prices, depth, and full ask levels at best ask
        yes_ask, yes_bid, yes_ask_depth, yes_ask_levels = clob_prices.get(team_token_id, (None, None, None, []))
        no_ask, no_bid, no_ask_depth, no_ask_levels = clob_prices.get(opp_token_id, (None, None, None, [])) if opp_token_id else (None, None, None, [])

        # Synthetic unique platform_id per team entry
        synthetic_id = f"{condition_id}_{team_norm}"

        results.append(NormalizedMarket(
            platform=Platform.POLYMARKET,
            platform_id=synthetic_id,
            platform_url=platform_url,
            raw_question=question,
            market_type=MarketType.SPORTS,
            asset=sport_code,
            direction="WIN",
            threshold=0.0,
            team=team_norm,
            opponent=opp_norm,
            sport=sport_code,
            # "moneyline" = full series/match winner; "child_moneyline" = per map/game winner
            sport_subtype="map" if sports_type == "child_moneyline" else "series",
            event_id=condition_id,    # condition_id groups both team entries
            map_number=_extract_map_number(question),
            resolution_dt=resolution_dt,
            yes_ask_cents=yes_ask,
            no_ask_cents=no_ask,
            yes_bid_cents=yes_bid,
            no_bid_cents=no_bid,
            yes_ask_depth=yes_ask_depth,
            no_ask_depth=no_ask_depth,
            yes_ask_levels=yes_ask_levels,
            no_ask_levels=no_ask_levels,
            yes_token_id=team_token_id,
            no_token_id=opp_token_id,
            liquidity_usd=float(gm.get("liquidity") or 0),
            volume_usd=float(gm.get("volume") or 0),
            raw_data=gm,
        ))

    return results


def _normalize_crypto_market(
    gm: dict[str, Any],
    condition_id: str,
    question: str,
    resolution_dt: datetime,
    platform_url: str,
) -> NormalizedMarket | None:
    """Normalize a Polymarket crypto/binary YES-NO market."""
    asset = extract_asset(question)
    direction = extract_direction(question)
    threshold = extract_dollar_amount(question)

    if asset is None or direction is None or threshold is None:
        return None

    yes_id, no_id = _extract_yes_no_token_ids(gm)
    clob_prices: dict[str, tuple[float | None, float | None]] = gm.get("_clob_prices", {})

    yes_ask = yes_bid = no_ask = no_bid = None
    yes_ask_depth = no_ask_depth = None
    yes_ask_levels: list[tuple[float, float]] = []
    no_ask_levels:  list[tuple[float, float]] = []
    if yes_id:
        yes_ask, yes_bid, yes_ask_depth, yes_ask_levels = clob_prices.get(yes_id, (None, None, None, []))
    if no_id:
        no_ask, no_bid, no_ask_depth, no_ask_levels = clob_prices.get(no_id, (None, None, None, []))

    return NormalizedMarket(
        platform=Platform.POLYMARKET,
        platform_id=condition_id,
        platform_url=platform_url,
        raw_question=question,
        market_type=MarketType.CRYPTO,
        asset=asset,
        direction=direction,
        threshold=threshold,
        resolution_dt=resolution_dt,
        yes_ask_cents=yes_ask,
        no_ask_cents=no_ask,
        yes_bid_cents=yes_bid,
        no_bid_cents=no_bid,
        yes_ask_depth=yes_ask_depth,
        no_ask_depth=no_ask_depth,
        yes_ask_levels=yes_ask_levels,
        no_ask_levels=no_ask_levels,
        yes_token_id=yes_id,
        no_token_id=no_id,
        liquidity_usd=float(gm.get("liquidity") or 0),
        volume_usd=float(gm.get("volume") or 0),
        raw_data=gm,
    )


def _extract_all_token_ids(gm: dict[str, Any]) -> list[str]:
    """Extract all CLOB token IDs from a Gamma market (for pre-fetching)."""
    token_ids = _parse_json_field(gm.get("clobTokenIds")) or []
    return [str(tid) for tid in token_ids if tid]


def _extract_yes_no_token_ids(gm: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Extract YES and NO token IDs from a binary Gamma market dict.
    clobTokenIds is a stringified JSON array: [yes_token_id, no_token_id]
    outcomes is a stringified JSON array: ["Yes", "No"]
    """
    token_ids = _parse_json_field(gm.get("clobTokenIds")) or []
    outcomes = _parse_json_field(gm.get("outcomes")) or ["Yes", "No"]

    if len(token_ids) < 2:
        return None, None

    yes_idx = None
    no_idx = None
    for i, o in enumerate(outcomes):
        o_lower = str(o).lower()
        if o_lower in ("yes", "true", "1"):
            yes_idx = i
        elif o_lower in ("no", "false", "0"):
            no_idx = i

    yes_id = token_ids[yes_idx] if yes_idx is not None and yes_idx < len(token_ids) else token_ids[0]
    no_id = token_ids[no_idx] if no_idx is not None and no_idx < len(token_ids) else token_ids[1]

    return str(yes_id), str(no_id)


def _parse_json_field(value: Any) -> list | None:
    """Parse a field that may be a stringified JSON list or already a list."""
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _is_yes_no_market(outcomes: list) -> bool:
    """Return True if outcomes is a binary YES/NO market."""
    if len(outcomes) != 2:
        return False
    lower = {str(o).lower() for o in outcomes}
    return lower == {"yes", "no"}


def _extract_series_slug(gm: dict[str, Any]) -> str:
    """
    Extract the Polymarket series slug from the embedded events array.

    The series slug (e.g. "nba-2026", "international-cricket", "atp") is the most
    reliable sport identifier for moneyline markets whose question text has no sport
    keyword (e.g. "Mavericks vs. Hornets").

    Lookup order:
      1. events[0].seriesSlug   (direct shortcut added by Gamma)
      2. events[0].series[0].slug  (full series object)
      3. events[0].ticker  (event ticker — often has sport prefix)
    """
    events = gm.get("events") or []
    if not events:
        return ""
    ev = events[0]
    slug = ev.get("seriesSlug") or ""
    if slug:
        return slug
    series = ev.get("series") or []
    if series:
        slug = series[0].get("slug") or ""
    if slug:
        return slug
    return ev.get("ticker") or ""


def _detect_sport_from_question(question: str) -> str | None:
    """Detect sport code from a market question string."""
    return _detect_sport_from_text(question)


def _detect_sport_from_text(text: str) -> str | None:
    """Detect sport code from arbitrary text."""
    t = text.lower()
    for keyword, code in sorted(_POLY_SPORT_MAP.items(), key=lambda x: -len(x[0])):
        if keyword in t:
            return code
    return None


def _detect_sport_from_series_slug(series_slug: str) -> str | None:
    """
    Detect sport code from a Polymarket series slug.

    Series slugs use hyphens as word separators (e.g. "nba-2026", "la-liga-2025",
    "international-cricket"). Replace hyphens with spaces and run through the
    standard sport keyword map.
    """
    if not series_slug:
        return None
    return _detect_sport_from_text(series_slug.replace("-", " "))


def _fetch_book(http: httpx.Client, token_id: str) -> tuple[float | None, float | None, float | None]:
    """
    Fetch CLOB orderbook for a single token and return (ask_cents, bid_cents, ask_depth).

    CLOB API:
    - bids sorted ASCENDING  → best bid = bids[-1]  (highest price)
    - asks sorted DESCENDING → best ask = asks[-1]  (lowest price / most competitive)
    Prices are 0-1 float strings → multiply by 100 for cents.
    ask_depth = total shares available at the best ask price level.
    """
    try:
        resp = http.get(f"{CLOB_API_URL}/book", params={"token_id": token_id})
        resp.raise_for_status()
        book = resp.json()

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = round(float(bids[-1]["price"]) * 100, 4) if bids else None

        if asks:
            # CLOB asks are sorted DESCENDING → best ask (lowest price) is last
            best_ask_entry = asks[-1]
            best_ask = round(float(best_ask_entry["price"]) * 100, 4)
            # Sum all size at the best ask price level (price may repeat across entries)
            best_ask_price_raw = best_ask_entry["price"]
            ask_depth = sum(
                float(a["size"])
                for a in asks
                if a.get("price") == best_ask_price_raw
            )
            ask_depth = round(ask_depth, 2)
            # Full ask ladder sorted ASCENDING (best/cheapest ask first)
            # Aggregate size per price level, then sort ascending.
            level_map: dict[float, float] = {}
            for a in asks:
                p = round(float(a["price"]) * 100, 4)
                level_map[p] = level_map.get(p, 0.0) + float(a["size"])
            ask_levels: list[tuple[float, float]] = sorted(level_map.items())
        else:
            best_ask = None
            ask_depth = None
            ask_levels = []

        return best_ask, best_bid, ask_depth, ask_levels
    except Exception:
        log.debug("CLOB fetch failed for token %s", token_id[:20], exc_info=True)
        return None, None, None, []


def _gamma_in_window(gm: dict[str, Any], now: datetime, cutoff: datetime) -> bool:
    """Return True if market closes within [now, cutoff]."""
    end_str = (gm.get("endDate") or gm.get("endDateIso") or "").strip()
    dt = parse_iso(end_str)
    if dt is None:
        return False
    return now < dt <= cutoff

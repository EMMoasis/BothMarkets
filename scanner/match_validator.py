"""
Validates that a sports match is actually scheduled before placing arb trades.

Uses Liquipedia (CS2 matches page) as the primary source — they explicitly allow
bots with a proper User-Agent and a 1-req/2s rate limit.  Results are cached for
30 minutes so the scan loop only makes one network call per cache window.

Return values from `is_match_scheduled()`:
  True  — both teams found in upcoming Liquipedia matches  → trade
  False — one or both teams NOT found                      → skip pair
  None  — Liquipedia unavailable (timeout, etc.)           → allow with warning
"""

from __future__ import annotations

import logging
import time
from difflib import SequenceMatcher
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_LIQUIPEDIA_URL = "https://liquipedia.net/counterstrike/Matches"
_HEADERS = {
    # Liquipedia ToS requires a descriptive User-Agent for bots.
    "User-Agent": "BothMarketsScanner/1.0 (educational arb research; respects rate limits)",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_CACHE_TTL_SECONDS = 1800   # 30 minutes between Liquipedia refreshes
_HTTP_TIMEOUT = 8           # seconds
_FUZZY_THRESHOLD = 0.72     # SequenceMatcher ratio to count as a match

# ---------------------------------------------------------------------------
# Module-level cache:  sport_key → (frozenset_of_team_names, fetched_at)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[frozenset[str], float]] = {}

# Per-pair result cache so we don't re-run fuzzy matching every 2s
# key: (team, opponent, sport)  →  (result: bool|None, cached_at: float)
_pair_cache: dict[tuple[str, str, str], tuple[Optional[bool], float]] = {}
_PAIR_CACHE_TTL = _CACHE_TTL_SECONDS   # same window as team list


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_match_scheduled(team: str, opponent: str, sport: str) -> Optional[bool]:
    """
    Check whether *team* vs *opponent* appears in Liquipedia's upcoming matches.

    Currently only implemented for CS2.  All other sports return None (allow).

    Returns:
        True   — verified on Liquipedia            → safe to trade
        False  — not found on Liquipedia           → skip pair
        None   — Liquipedia unavailable / unknown  → allow with warning
    """
    if sport.upper() != "CS2":
        return None  # Validation not implemented for this sport yet

    if not team or not opponent:
        return None  # Defensive: can't validate empty names

    pair_key = (team.lower(), opponent.lower(), sport.upper())
    now = time.monotonic()

    # Return cached pair result within TTL
    if pair_key in _pair_cache:
        result, cached_at = _pair_cache[pair_key]
        if now - cached_at < _PAIR_CACHE_TTL:
            return result

    # Get (or refresh) the full team list
    team_set = _get_cached_team_list(sport, now)
    if team_set is None:
        log.warning(
            "match_validator | Liquipedia unavailable — allowing %s vs %s unverified",
            team, opponent,
        )
        result = None
    else:
        found_a = _fuzzy_find(team, team_set)
        found_b = _fuzzy_find(opponent, team_set)

        if found_a and found_b:
            log.debug("match_validator | Verified: %s vs %s", team, opponent)
            result = True
        else:
            missing = [t for t, found in [(team, found_a), (opponent, found_b)] if not found]
            log.warning(
                "match_validator | NOT scheduled on Liquipedia — %s — pair will be skipped",
                ", ".join(missing),
            )
            result = False

    _pair_cache[pair_key] = (result, now)
    return result


def clear_cache() -> None:
    """Force a fresh Liquipedia fetch on the next validation call (used in tests)."""
    _cache.clear()
    _pair_cache.clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_cached_team_list(sport: str, now: float) -> Optional[frozenset[str]]:
    """Return cached team set or fetch a fresh one. None = unavailable."""
    key = sport.upper()
    if key in _cache:
        teams, fetched_at = _cache[key]
        if now - fetched_at < _CACHE_TTL_SECONDS:
            return teams

    teams = _fetch_liquipedia_teams()
    if teams is not None:
        _cache[key] = (teams, now)
    return teams


def _fetch_liquipedia_teams() -> Optional[frozenset[str]]:
    """
    HTTP GET the Liquipedia CS2 Matches page and extract all upcoming team names.
    Returns None on any error (caller treats as unavailable).
    """
    try:
        resp = requests.get(_LIQUIPEDIA_URL, headers=_HEADERS, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            log.warning("match_validator | Liquipedia returned HTTP %d", resp.status_code)
            return None

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        teams: set[str] = set()
        # Liquipedia match rows: team names appear inside .team-left / .team-right,
        # each typically contains a <span> with the display name.
        for sel in (
            ".team-left span",
            ".team-right span",
            ".matchTeamName",
            ".team-template-text",
        ):
            for el in soup.select(sel):
                name = el.get_text(strip=True)
                if name and name.upper() not in ("TBD", "TBA", ""):
                    teams.add(name)

        log.info("match_validator | Fetched %d team names from Liquipedia", len(teams))
        return frozenset(teams) if teams else None

    except requests.Timeout:
        log.warning("match_validator | Liquipedia request timed out")
        return None
    except Exception as exc:
        log.warning("match_validator | Liquipedia fetch failed: %s", exc)
        return None


def _fuzzy_find(name: str, team_set: frozenset[str]) -> bool:
    """
    Returns True if *name* fuzzy-matches any entry in *team_set* above threshold,
    OR if one is a substring of the other (handles short aliases like 'ShindeN').
    """
    name_l = name.lower().strip()
    if not name_l:
        return False
    for t in team_set:
        t_l = t.lower()
        # Exact substring match (handles aliases)
        if name_l in t_l or t_l in name_l:
            return True
        # Fuzzy ratio
        if SequenceMatcher(None, name_l, t_l).ratio() >= _FUZZY_THRESHOLD:
            return True
    return False

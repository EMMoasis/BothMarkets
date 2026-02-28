"""
Validates that a sports match is actually scheduled before placing arb trades.

Uses the Liquipedia API v3 (https://api.liquipedia.net/) as the primary source.
A free API key is required — register at https://api.liquipedia.net/ and set:

    LIQUIPEDIA_API_KEY=<your_key>   in your .env file

If the key is not set, validation is skipped and all matches are allowed through
with a one-time warning.  Results are cached for 30 minutes so the scan loop
makes at most one API call per sport per cache window.

Supported sports (validated against Liquipedia):
  CS2, LOL, VALORANT, DOTA2, RL

Unsupported sports (traditional / not on Liquipedia):
  NBA, NFL, NHL, MLB, SOCCER — these return None (allow through, no warning)

Return values from `is_match_scheduled()`:
  True  — both teams found in upcoming Liquipedia matches  → trade
  False — one or both teams NOT found                      → skip pair
  None  — Liquipedia unavailable / key missing             → allow with warning
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Liquipedia API v3 — sport key → wiki slug
# ---------------------------------------------------------------------------
_LIQUIPEDIA_SPORT_WIKIS: dict[str, str] = {
    "CS2":      "counterstrike",
    "LOL":      "leagueoflegends",
    "VALORANT": "valorant",
    "DOTA2":    "dota2",
    "RL":       "rocketleague",
}

# Exported so callers can check support without importing internals
SUPPORTED_SPORTS: frozenset[str] = frozenset(_LIQUIPEDIA_SPORT_WIKIS.keys())

_API_BASE     = "https://api.liquipedia.net/api/v3"
_CACHE_TTL_SECONDS = 1800   # 30 minutes between API refreshes
_HTTP_TIMEOUT      = 12     # seconds
_FUZZY_THRESHOLD   = 0.72   # SequenceMatcher ratio to count as a match
# How far ahead to look for matches (hours)
_LOOKAHEAD_HOURS   = 72

# ---------------------------------------------------------------------------
# Module-level cache:  sport_key → (frozenset_of_team_names, fetched_at)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[frozenset[str], float]] = {}

# Per-pair result cache so we don't re-run fuzzy matching every 2s
# key: (team, opponent, sport)  →  (result: bool|None, cached_at: float)
_pair_cache: dict[tuple[str, str, str], tuple[Optional[bool], float]] = {}
_PAIR_CACHE_TTL = _CACHE_TTL_SECONDS

# One-time warning flag so we don't spam the log every cycle
_no_key_warned = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_match_scheduled(team: str, opponent: str, sport: str) -> Optional[bool]:
    """
    Check whether *team* vs *opponent* appears in Liquipedia's upcoming matches.

    Supported sports: CS2, LOL, VALORANT, DOTA2, RL
    Unsupported sports (NBA, NFL, etc.) return None immediately (allow through).

    Returns:
        True   — verified on Liquipedia            → safe to trade
        False  — not found on Liquipedia           → skip pair
        None   — Liquipedia unavailable / sport not supported → allow with warning
    """
    global _no_key_warned

    sport_upper = sport.upper()

    if sport_upper not in SUPPORTED_SPORTS:
        return None  # Validation not implemented for this sport

    if not team or not opponent:
        return None  # Defensive: can't validate empty names

    # If no API key configured, warn once and allow everything through
    api_key = _get_api_key()
    if not api_key:
        if not _no_key_warned:
            log.warning(
                "match_validator | LIQUIPEDIA_API_KEY not set — "
                "match validation disabled. Register free key at https://api.liquipedia.net/"
            )
            _no_key_warned = True
        return None

    pair_key = (team.lower(), opponent.lower(), sport_upper)
    now = time.monotonic()

    # Return cached pair result within TTL
    if pair_key in _pair_cache:
        result, cached_at = _pair_cache[pair_key]
        if now - cached_at < _PAIR_CACHE_TTL:
            return result

    # Get (or refresh) the full team list for this sport
    team_set = _get_cached_team_list(sport_upper, now)
    if team_set is None:
        log.warning(
            "match_validator | Liquipedia unavailable (%s) — allowing %s vs %s unverified",
            sport_upper, team, opponent,
        )
        result = None
    else:
        found_a = _fuzzy_find(team, team_set)
        found_b = _fuzzy_find(opponent, team_set)

        if found_a and found_b:
            log.debug("match_validator | Verified: %s vs %s (%s)", team, opponent, sport_upper)
            result = True
        else:
            missing = [t for t, found in [(team, found_a), (opponent, found_b)] if not found]
            log.warning(
                "match_validator | NOT scheduled on Liquipedia (%s) — %s — pair will be skipped",
                sport_upper, ", ".join(missing),
            )
            result = False

    _pair_cache[pair_key] = (result, now)
    return result


def clear_cache() -> None:
    """Force a fresh Liquipedia fetch on the next validation call (used in tests)."""
    global _no_key_warned
    _cache.clear()
    _pair_cache.clear()
    _no_key_warned = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    """Read LIQUIPEDIA_API_KEY from env. Returns empty string if not set."""
    return os.environ.get("LIQUIPEDIA_API_KEY", "").strip()


def _get_cached_team_list(sport: str, now: float) -> Optional[frozenset[str]]:
    """Return cached team set or fetch a fresh one. None = unavailable."""
    key = sport.upper()
    if key in _cache:
        teams, fetched_at = _cache[key]
        if now - fetched_at < _CACHE_TTL_SECONDS:
            return teams

    wiki = _LIQUIPEDIA_SPORT_WIKIS[key]
    teams = _fetch_liquipedia_teams_api(wiki)
    if teams is not None:
        _cache[key] = (teams, now)
    return teams


def _fetch_liquipedia_teams_api(wiki: str) -> Optional[frozenset[str]]:
    """
    Fetch upcoming match participants via the Liquipedia API v3.

    Uses the /match endpoint with a time window of now → +72h.
    Extracts all team names from match2opponents[].name.

    Returns a frozenset of team names, or None on error.
    """
    api_key = _get_api_key()
    if not api_key:
        return None

    now_utc = datetime.now(timezone.utc)
    cutoff   = now_utc + timedelta(hours=_LOOKAHEAD_HOURS)
    date_from = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    date_to   = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    try:
        resp = requests.get(
            f"{_API_BASE}/match",
            params={
                "wiki":       wiki,
                "conditions": (
                    f"[[date_time_utc::>{date_from}]] "
                    f"AND [[date_time_utc::<{date_to}]]"
                ),
                "query":  "match2opponents",
                "limit":  "500",
                "order":  "date_time_utc ASC",
            },
            headers={
                "Authorization": f"Apikey {api_key}",
                "User-Agent":    "BothMarketsScanner/1.0 (educational arb research)",
                "Accept":        "application/json",
            },
            timeout=_HTTP_TIMEOUT,
        )

        if resp.status_code == 429:
            log.warning("match_validator | Liquipedia API rate-limited (429) for wiki=%s", wiki)
            return None
        if resp.status_code == 401 or resp.status_code == 403:
            log.warning(
                "match_validator | Liquipedia API key rejected (HTTP %d) — "
                "check LIQUIPEDIA_API_KEY in .env",
                resp.status_code,
            )
            return None
        if resp.status_code != 200:
            log.warning("match_validator | Liquipedia API returned HTTP %d for wiki=%s", resp.status_code, wiki)
            return None

        data = resp.json()
        matches = data.get("result", [])

        teams: set[str] = set()
        for match in matches:
            for opp in match.get("match2opponents", []):
                name = (opp.get("name") or "").strip()
                if name and name.upper() not in ("TBD", "TBA", ""):
                    teams.add(name)

        log.info(
            "match_validator | API returned %d matches → %d team names (wiki=%s)",
            len(matches), len(teams), wiki,
        )
        return frozenset(teams) if teams else None

    except requests.Timeout:
        log.warning("match_validator | Liquipedia API timed out (wiki=%s)", wiki)
        return None
    except Exception as exc:
        log.warning("match_validator | Liquipedia API fetch failed (wiki=%s): %s", wiki, exc)
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

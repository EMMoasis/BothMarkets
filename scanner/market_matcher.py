"""
Cross-platform market matching engine.

Uses strict matching — ALL criteria must pass or the pair is rejected.

For SPORTS markets (6 criteria):
  1. Same sport code (e.g., "CS2", "LOL", "VALORANT")
  2. Same team (normalized name match)
  3. Same opponent (normalized name match) — prevents DRX vs TeamA matching DRX vs TeamB
  4. Resolution datetime within ±RESOLUTION_TIME_TOLERANCE_HOURS
  5. Same sport_subtype ("series" = match winner, "map" = per-map/game winner)
     Prevents matching Kalshi "KXLOLGAME series winner" against Polymarket "Game 3 winner".
  6. Same map/game number when both markets specify one (e.g., map 2 ≠ game 3)

For CRYPTO markets — DISABLED by default (CRYPTO_MATCHING_ENABLED = False):
  Kalshi resolves via CF Benchmarks BRTI (60-sec multi-exchange average).
  Polymarket resolves via Binance 1-min candle BTC/USDT close.
  These are DIFFERENT oracles: they can diverge at settlement, so a "covered"
  position is NOT risk-free. Additionally there is a structural ~5-hour gap
  between platform expiry times. See config.CRYPTO_MATCHING_ENABLED.

  When enabled, checks 4 criteria:
  1. Exact same asset (e.g., "BTC")
  2. Same direction ("ABOVE" or "BELOW")
  3. Resolution datetime within ±RESOLUTION_TIME_TOLERANCE_HOURS
  4. Exact same numeric threshold (e.g., 90000.0)
"""

from __future__ import annotations

import logging
from datetime import timedelta

from scanner.config import CRYPTO_MATCHING_ENABLED, RESOLUTION_TIME_TOLERANCE_HOURS, RESOLUTION_TIME_TOLERANCE_HOURS_SPORTS
from scanner.models import MarketType, MatchedPair, NormalizedMarket

log = logging.getLogger(__name__)


class MarketMatcher:
    """
    Finds matched pairs between Kalshi and Polymarket markets.

    Handles both CRYPTO and SPORTS market types with appropriate matching rules.
    Each Kalshi market and each Polymarket market appears in at most one pair.
    """

    def find_matches(
        self,
        kalshi_markets: list[NormalizedMarket],
        poly_markets: list[NormalizedMarket],
    ) -> list[MatchedPair]:
        """
        Compare all Kalshi markets against all Polymarket markets.
        Returns only pairs that satisfy all matching criteria for their type.

        Logs every matched pair with platform URLs for test visibility.
        """
        if not kalshi_markets or not poly_markets:
            log.info("MarketMatcher: empty input — K:%d P:%d", len(kalshi_markets), len(poly_markets))
            return []

        # Separate by market type
        k_crypto = [m for m in kalshi_markets if m.market_type == MarketType.CRYPTO]
        k_sports = [m for m in kalshi_markets if m.market_type == MarketType.SPORTS]
        p_crypto = [m for m in poly_markets if m.market_type == MarketType.CRYPTO]
        p_sports = [m for m in poly_markets if m.market_type == MarketType.SPORTS]

        log.info(
            "MarketMatcher: K(%d crypto, %d sports) × P(%d crypto, %d sports)",
            len(k_crypto), len(k_sports), len(p_crypto), len(p_sports),
        )

        pairs: list[MatchedPair] = []
        used_kalshi: set[str] = set()
        used_poly: set[str] = set()

        # Match crypto markets (disabled by default — different oracles, see module docstring)
        if CRYPTO_MATCHING_ENABLED:
            crypto_pairs = self._match_crypto(k_crypto, p_crypto, used_kalshi, used_poly)
            pairs.extend(crypto_pairs)
        else:
            crypto_pairs = []
            log.debug(
                "Crypto matching disabled (CRYPTO_MATCHING_ENABLED=False). "
                "K=%d crypto markets, P=%d crypto markets skipped.",
                len(k_crypto), len(p_crypto),
            )

        # Match sports markets
        sports_pairs = self._match_sports(k_sports, p_sports, used_kalshi, used_poly)
        pairs.extend(sports_pairs)

        log.info(
            "MarketMatcher: total %d matched pairs (%d crypto, %d sports)",
            len(pairs), len(crypto_pairs), len(sports_pairs),
        )
        return pairs

    def _match_crypto(
        self,
        kalshi: list[NormalizedMarket],
        poly: list[NormalizedMarket],
        used_kalshi: set[str],
        used_poly: set[str],
    ) -> list[MatchedPair]:
        """
        Match crypto markets using 4 strict criteria:
        asset + direction + threshold + resolution_dt (±1h).
        """
        # Pre-group Polymarket by (asset, direction) for fast lookup
        poly_index: dict[tuple[str, str], list[NormalizedMarket]] = {}
        for pm in poly:
            key = (pm.asset, pm.direction)
            poly_index.setdefault(key, []).append(pm)

        pairs: list[MatchedPair] = []
        rejected: dict[str, int] = {}

        for km in kalshi:
            if km.platform_id in used_kalshi:
                continue

            candidates = poly_index.get((km.asset, km.direction), [])
            for pm in candidates:
                if pm.platform_id in used_poly:
                    continue

                reason = _check_crypto_match(km, pm)
                if reason is not None:
                    rejected[reason] = rejected.get(reason, 0) + 1
                    continue

                pair = MatchedPair(kalshi=km, poly=pm)
                pairs.append(pair)
                used_kalshi.add(km.platform_id)
                used_poly.add(pm.platform_id)

                log.info(
                    "MATCH | CRYPTO | %s %s $%.0f | closes ~%s UTC\n"
                    "  Kalshi:     %s\n"
                    "  Polymarket: %s\n"
                    "  K-Q: %s\n"
                    "  P-Q: %s",
                    km.asset, km.direction, km.threshold,
                    km.resolution_dt.strftime("%Y-%m-%d %H:%M"),
                    km.platform_url,
                    pm.platform_url,
                    km.raw_question[:100],
                    pm.raw_question[:100],
                )
                break

        log.info(
            "Crypto matching: %d × %d → %d pairs | rejections: %s",
            len(kalshi), len(poly), len(pairs),
            ", ".join(f"{k}={v}" for k, v in rejected.items()) or "none",
        )
        return pairs

    def _match_sports(
        self,
        kalshi: list[NormalizedMarket],
        poly: list[NormalizedMarket],
        used_kalshi: set[str],
        used_poly: set[str],
    ) -> list[MatchedPair]:
        """
        Match sports markets using 4 strict criteria:
          - Same sport code
          - Same normalized team name
          - Resolution datetime within ±RESOLUTION_TIME_TOLERANCE_HOURS
          - Same sport_subtype ("series" vs "map") — prevents cross-type false matches

        The Kalshi market for "Team A wins" (YES market) maps to the Polymarket
        per-team entry for "Team A wins" (yes_token_id = Team A's token).
        """
        # Pre-group Polymarket sports markets by (sport, team, sport_subtype) for fast lookup
        poly_index: dict[tuple[str, str, str], list[NormalizedMarket]] = {}
        for pm in poly:
            key = (pm.sport, pm.team, pm.sport_subtype)
            poly_index.setdefault(key, []).append(pm)

        pairs: list[MatchedPair] = []
        rejected: dict[str, int] = {}
        no_candidates: dict[str, int] = {}   # subtype → count of Kalshi markets with 0 Poly candidates

        for km in kalshi:
            if km.platform_id in used_kalshi:
                continue

            # Look for Poly markets with same sport + team + subtype
            candidates = poly_index.get((km.sport, km.team, km.sport_subtype), [])

            if not candidates:
                key = km.sport_subtype
                no_candidates[key] = no_candidates.get(key, 0) + 1
                log.debug(
                    "NO CANDIDATES | %s | %s | %s vs %s | subtype=%s — no Poly %s market for this team",
                    km.sport, km.platform_id, km.team, km.opponent, km.sport_subtype, km.sport_subtype,
                )
                continue

            for pm in candidates:
                if pm.platform_id in used_poly:
                    continue

                reason = _check_sports_match(km, pm)
                if reason is not None:
                    rejected[reason] = rejected.get(reason, 0) + 1
                    continue

                pair = MatchedPair(kalshi=km, poly=pm)
                pairs.append(pair)
                used_kalshi.add(km.platform_id)
                used_poly.add(pm.platform_id)

                log.info(
                    "MATCH | SPORTS | %s | %s vs %s | closes ~%s UTC\n"
                    "  Kalshi:     %s\n"
                    "  Polymarket: %s\n"
                    "  K-Q: %s\n"
                    "  P-Q: %s",
                    km.sport, km.team, km.opponent,
                    km.resolution_dt.strftime("%Y-%m-%d %H:%M"),
                    km.platform_url,
                    pm.platform_url,
                    km.raw_question[:100],
                    pm.raw_question[:100],
                )
                break

        no_cand_str = ", ".join(f"{k}_no_poly={v}" for k, v in sorted(no_candidates.items()))
        log.info(
            "Sports matching: %d × %d → %d pairs | rejections: %s | no_candidates: %s",
            len(kalshi), len(poly), len(pairs),
            ", ".join(f"{k}={v}" for k, v in rejected.items()) or "none",
            no_cand_str or "none",
        )
        return pairs


# ------------------------------------------------------------------
# Match-check functions
# ------------------------------------------------------------------

def _check_crypto_match(km: NormalizedMarket, pm: NormalizedMarket) -> str | None:
    """
    Check all 4 criteria for a crypto market pair.
    Returns None if all pass, or name of first failing criterion.
    """
    if km.asset != pm.asset:
        return "asset"
    if km.direction != pm.direction:
        return "direction"
    time_diff = abs((km.resolution_dt - pm.resolution_dt).total_seconds())
    if time_diff > RESOLUTION_TIME_TOLERANCE_HOURS * 3600:
        return "date"
    if km.threshold != pm.threshold:
        return "threshold"
    return None


def _check_sports_match(km: NormalizedMarket, pm: NormalizedMarket) -> str | None:
    """
    Check all 6 criteria for a sports market pair.
    Returns None if all pass, or name of first failing criterion.

    Criteria:
      1. sport      - same sport code (CS2, LOL, etc.)
      2. team       - same normalized team name (the team this YES contract is for)
      3. opponent   - same normalized opponent — prevents DRX vs TeamA matching DRX vs TeamB
                      when the same team plays twice in the scan window. Skipped when
                      either market has no opponent set.
      4. date       - resolution_dt within ±RESOLUTION_TIME_TOLERANCE_HOURS
      5. subtype    - same sport_subtype ("series" vs "map") — prevents
                      matching a series winner market against a per-map market
      6. map_number - same map/game number when both markets specify one
    """
    if km.sport != pm.sport:
        return "sport"
    if km.team != pm.team:
        return "team"
    # Opponent check: both must play the same opposing team.
    # Skip only when a market genuinely has no opponent (shouldn't happen for 2-team markets).
    if km.opponent and pm.opponent and km.opponent != pm.opponent:
        return "opponent"
    time_diff = abs((km.resolution_dt - pm.resolution_dt).total_seconds())
    if time_diff > RESOLUTION_TIME_TOLERANCE_HOURS_SPORTS * 3600:
        return "date"
    if km.sport_subtype != pm.sport_subtype:
        return "subtype"
    if km.map_number is not None and pm.map_number is not None:
        if km.map_number != pm.map_number:
            return "map_number"
    return None


# Kept for backward-compat with existing tests
def _check_match(km: NormalizedMarket, pm: NormalizedMarket) -> str | None:
    """
    Check match criteria based on market type.
    Dispatches to _check_crypto_match or _check_sports_match.
    """
    if km.market_type == MarketType.SPORTS:
        return _check_sports_match(km, pm)
    return _check_crypto_match(km, pm)

"""
Cross-platform market matching engine.

Uses strict matching — ALL criteria must pass or the pair is rejected.

For CRYPTO markets (4 criteria):
  1. Exact same asset (e.g., "BTC")
  2. Same direction ("ABOVE" or "BELOW")
  3. Resolution datetime within ±RESOLUTION_TIME_TOLERANCE_HOURS
  4. Exact same numeric threshold (e.g., 90000.0)

For SPORTS markets (3 criteria):
  1. Same sport code (e.g., "CS2", "NBA")
  2. Same team (normalized name match)
  3. Resolution datetime within ±RESOLUTION_TIME_TOLERANCE_HOURS
  (opponent is used for display/log but not strict matching since Poly
   may list same match with slightly different wording)
"""

from __future__ import annotations

import logging
from datetime import timedelta

from scanner.config import RESOLUTION_TIME_TOLERANCE_HOURS
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

        # Match crypto markets
        crypto_pairs = self._match_crypto(k_crypto, p_crypto, used_kalshi, used_poly)
        pairs.extend(crypto_pairs)

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
        Match sports markets:
          - Same sport code
          - Same normalized team name
          - Resolution datetime within ±RESOLUTION_TIME_TOLERANCE_HOURS

        The Kalshi market for "Team A wins" (YES market) maps to the Polymarket
        per-team entry for "Team A wins" (yes_token_id = Team A's token).
        """
        # Pre-group Polymarket sports markets by (sport, team) for fast lookup
        poly_index: dict[tuple[str, str], list[NormalizedMarket]] = {}
        for pm in poly:
            key = (pm.sport, pm.team)
            poly_index.setdefault(key, []).append(pm)

        pairs: list[MatchedPair] = []
        rejected: dict[str, int] = {}

        for km in kalshi:
            if km.platform_id in used_kalshi:
                continue

            # Look for Poly markets with same sport + team
            candidates = poly_index.get((km.sport, km.team), [])

            # Also try with opponent (Poly might list teams in different order)
            if not candidates:
                # Try to find by checking all poly markets for this sport
                candidates = [
                    pm for pm in poly
                    if pm.sport == km.sport and pm.team == km.team
                ]

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

        log.info(
            "Sports matching: %d × %d → %d pairs | rejections: %s",
            len(kalshi), len(poly), len(pairs),
            ", ".join(f"{k}={v}" for k, v in rejected.items()) or "none",
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
    Check matching criteria for a sports market pair.
    Returns None if all pass, or name of first failing criterion.
    """
    if km.sport != pm.sport:
        return "sport"
    if km.team != pm.team:
        return "team"
    time_diff = abs((km.resolution_dt - pm.resolution_dt).total_seconds())
    if time_diff > RESOLUTION_TIME_TOLERANCE_HOURS * 3600:
        return "date"
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

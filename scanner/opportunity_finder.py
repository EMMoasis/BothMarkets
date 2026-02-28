"""
Arbitrage opportunity detection and formatting.

For each matched pair, evaluates two cross-platform strategies:
  Strategy A: Buy Kalshi YES + Buy Polymarket NO
  Strategy B: Buy Kalshi NO + Buy Polymarket YES

For SPORTS markets:
  "YES" = this team wins, "NO" = opponent wins (the other token)

Reports all strategies where combined ask cost < 100c.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from scanner.config import (
    MATCH_VALIDATION_ENABLED,
    MIN_PRICE_CENTS,
    MIN_SPREAD_CENTS,
    PROFIT_TIERS,
    SKIP_UNVERIFIED_MATCHES,
)
from scanner.match_validator import SUPPORTED_SPORTS, is_match_scheduled
from scanner.models import MarketType, MatchedPair, NormalizedMarket, Opportunity

log = logging.getLogger(__name__)


class OpportunityFinder:
    """
    Evaluates matched pairs for cross-platform arbitrage.

    An opportunity exists when:
      kalshi_side_ask_cents + poly_other_side_ask_cents < 100c
    """

    def find_opportunities(self, pairs: list[MatchedPair]) -> list[Opportunity]:
        """
        Evaluate both strategies for every matched pair.
        Returns profitable opportunities sorted by spread descending (best first).
        """
        opportunities: list[Opportunity] = []

        for pair in pairs:
            km = pair.kalshi
            pm = pair.poly

            # --- Match validation (sports only) ---
            # Verify the match actually appears on Liquipedia's upcoming schedule.
            # Avoids arb losses on cancelled / never-scheduled events.
            # Supported esports: CS2, LOL, VALORANT, DOTA2, RL.
            # Traditional sports (NBA/NFL/etc.) pass through silently at DEBUG.
            if MATCH_VALIDATION_ENABLED and km.market_type == MarketType.SPORTS:
                sport_key = (km.sport or "").upper()
                if sport_key not in SUPPORTED_SPORTS:
                    # No Liquipedia validation for this sport — allow, no warning spam
                    log.debug(
                        "SKIP VALIDATION | %s vs %s — %s validation not supported, allowing",
                        km.team, km.opponent, km.sport,
                    )
                else:
                    verified = is_match_scheduled(km.team, km.opponent, km.sport)
                    if verified is False:
                        if SKIP_UNVERIFIED_MATCHES:
                            log.info(
                                "SKIP | %s vs %s (%s) not found on Liquipedia — pair skipped",
                                km.team, km.opponent, km.sport,
                            )
                            continue
                        else:
                            log.warning(
                                "WARN | %s vs %s (%s) not found on Liquipedia — allowing (SKIP_UNVERIFIED_MATCHES=False)",
                                km.team, km.opponent, km.sport,
                            )
                    elif verified is None:
                        log.warning(
                            "WARN | Could not verify %s vs %s (%s) — Liquipedia unavailable, allowing",
                            km.team, km.opponent, km.sport,
                        )

            # Strategy A: Buy Kalshi YES + Buy Polymarket NO
            # CRYPTO: Kalshi YES (above threshold) + Poly NO (below threshold)
            # SPORTS: Kalshi YES (team A wins) + Poly NO (= opponent wins token)
            opp_a = _evaluate_strategy(
                pair=pair,
                kalshi_cost=km.yes_ask_cents,
                poly_cost=pm.no_ask_cents,
                kalshi_side="YES",
                poly_side="NO",
            )
            if opp_a is not None:
                opportunities.append(opp_a)

            # Strategy B: Buy Kalshi NO + Buy Polymarket YES
            # CRYPTO: Kalshi NO (below threshold) + Poly YES (above threshold)
            # SPORTS: Kalshi NO (team A loses) + Poly YES (= team A wins token, i.e. consistent)
            opp_b = _evaluate_strategy(
                pair=pair,
                kalshi_cost=km.no_ask_cents,
                poly_cost=pm.yes_ask_cents,
                kalshi_side="NO",
                poly_side="YES",
            )
            if opp_b is not None:
                opportunities.append(opp_b)

        opportunities.sort(key=lambda o: o.spread_cents, reverse=True)

        log.info(
            "OpportunityFinder: %d pairs → %d opportunities",
            len(pairs), len(opportunities),
        )
        return opportunities

    def log_pair_prices(self, pair: MatchedPair) -> None:
        """
        Log a matched pair with current prices, depth, and strategy evaluation.
        Called every price poll cycle for all matched pairs (even without arb).
        Pairs where Kalshi has no prices at all are logged at DEBUG to reduce noise.
        """
        km = pair.kalshi
        pm = pair.poly

        # If Kalshi has no prices on either side, there's nothing actionable —
        # log at DEBUG only to avoid flooding the log with N/A spam.
        if km.yes_ask_cents is None and km.no_ask_cents is None:
            log.debug("PAIR (no Kalshi prices) | %s | skipping verbose log", km.platform_id)
            return

        def _fmt_k(cents, depth) -> str:
            """Format Kalshi price with orderbook contract depth."""
            price = f"{cents:.1f}c" if cents is not None else "N/A"
            dep   = f"[{depth:.0f}ct]" if depth is not None else ""
            return f"{price}{dep}"

        def _fmt_p(cents, depth) -> str:
            """Format Polymarket price with orderbook depth in shares."""
            price = f"{cents:.1f}c" if cents is not None else "N/A"
            dep   = f"[{depth:.0f}sh]" if depth is not None else ""
            return f"{price}{dep}"

        k_yes = _fmt_k(km.yes_ask_cents, km.yes_ask_depth)
        k_no  = _fmt_k(km.no_ask_cents,  km.no_ask_depth)
        p_yes = _fmt_p(pm.yes_ask_cents, pm.yes_ask_depth)
        p_no  = _fmt_p(pm.no_ask_cents,  pm.no_ask_depth)

        strat_a = _combined_str(km.yes_ask_cents, pm.no_ask_cents, "K-YES + P-NO")
        strat_b = _combined_str(km.no_ask_cents, pm.yes_ask_cents, "K-NO  + P-YES")

        # Build market label depending on type
        if km.market_type == MarketType.SPORTS:
            market_label = f"{km.sport} | {km.team} vs {km.opponent}"
        else:
            market_label = f"{km.asset} {km.direction} ${km.threshold:.0f}"

        log.debug(
            "PAIR  | %s | %s UTC\n"
            "  Kalshi:     %s\n"
            "  Polymarket: %s\n"
            "  K-YES-ask=%s  K-NO-ask=%s  P-YES-ask=%s  P-NO-ask=%s\n"
            "  %s\n"
            "  %s",
            market_label,
            km.resolution_dt.strftime("%Y-%m-%d %H:%M"),
            km.platform_url,
            pm.platform_url,
            k_yes, k_no, p_yes, p_no,
            strat_a,
            strat_b,
        )


def format_opportunity_log(opp: Opportunity) -> str:
    """
    Format an opportunity as a multi-line log string with full details and URLs.

    CRYPTO example:
    ARB OPPORTUNITY | High | BTC ABOVE $90000 | spread=3.00c | 47.5h to close
      Strategy: Kalshi YES + Polymarket NO
      Kalshi:     https://kalshi.com/markets/KXBTC-26FEB21-T90000
      Polymarket: https://polymarket.com/event/btc-above-90k-feb21
      Cost: K-YES=57.0c + P-NO=40.0c = 97.0c combined → profit=3.0c per $1

    SPORTS example:
    ARB OPPORTUNITY | High | CS2 | M80 vs Voca | spread=8.00c | 2.5h to close
      Strategy: Kalshi YES (M80 wins) + Polymarket NO (Voca wins token)
      Kalshi:     https://kalshi.com/markets/KXCS2GAME-26FEB22M80VOC-M80
      Polymarket: https://polymarket.com/event/cs2-navij1-ksm-2026-02-21
      Cost: K-YES=57.0c + P-NO=35.0c = 92.0c combined → profit=8.0c per $1
    """
    km = opp.pair.kalshi
    pm = opp.pair.poly

    if km.market_type == MarketType.SPORTS:
        event_label = f"{km.sport} | {km.team} vs {km.opponent}"
        # Clarify what YES/NO means for sports
        if opp.kalshi_side == "YES":
            strategy_detail = (
                f"Kalshi YES ({km.team} wins) + "
                f"Polymarket NO ({km.opponent} wins token)"
            )
        else:
            strategy_detail = (
                f"Kalshi NO ({km.team} loses) + "
                f"Polymarket YES ({km.team} wins token)"
            )
    else:
        event_label = f"{km.asset} {km.direction} ${km.threshold:.0f}"
        strategy_detail = f"Kalshi {opp.kalshi_side} + Polymarket {opp.poly_side}"

    k_depth_str = f"{opp.kalshi_depth_shares:.0f} contracts" if opp.kalshi_depth_shares is not None else "no LOB data"
    p_depth_str = f"{opp.poly_depth_shares:.0f} shares"     if opp.poly_depth_shares  is not None else "?"

    return (
        f"ARB OPPORTUNITY | {opp.tier} | {event_label} | "
        f"spread={opp.spread_cents:.2f}c | {opp.hours_to_close:.1f}h to close\n"
        f"  Strategy: {strategy_detail}\n"
        f"  Kalshi:     {km.platform_url}\n"
        f"  Polymarket: {pm.platform_url}\n"
        f"  Cost: K-{opp.kalshi_side}={opp.kalshi_cost_cents:.1f}c [{k_depth_str}] + "
        f"P-{opp.poly_side}={opp.poly_cost_cents:.1f}c [{p_depth_str}] = "
        f"{opp.combined_cost_cents:.1f}c combined → profit={opp.spread_cents:.2f}c per $1"
    )


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _evaluate_strategy(
    pair: MatchedPair,
    kalshi_cost: float | None,
    poly_cost: float | None,
    kalshi_side: str,
    poly_side: str,
) -> Opportunity | None:
    """Evaluate one strategy direction. Returns Opportunity or None."""
    if kalshi_cost is None or poly_cost is None:
        return None

    # Skip near-zero prices: Poly min-order sizing becomes impossibly large
    if kalshi_cost < MIN_PRICE_CENTS or poly_cost < MIN_PRICE_CENTS:
        return None

    combined = kalshi_cost + poly_cost
    if combined >= 100.0:
        return None

    spread_cents = round(100.0 - combined, 4)
    if spread_cents < MIN_SPREAD_CENTS:
        return None

    tier = _classify_tier(spread_cents)
    if tier is None:
        return None

    now = datetime.now(timezone.utc)
    k_close = pair.kalshi.resolution_dt
    p_close = pair.poly.resolution_dt
    earlier_close = min(k_close, p_close)
    hours_to_close = max(0.0, (earlier_close - now).total_seconds() / 3600)

    # Depth at the relevant ask price
    km = pair.kalshi
    pm = pair.poly
    kalshi_depth = km.yes_ask_depth if kalshi_side == "YES" else km.no_ask_depth
    poly_depth   = pm.yes_ask_depth if poly_side   == "YES" else pm.no_ask_depth
    poly_levels  = pm.yes_ask_levels if poly_side  == "YES" else pm.no_ask_levels

    return Opportunity(
        pair=pair,
        kalshi_side=kalshi_side,
        poly_side=poly_side,
        kalshi_cost_cents=round(kalshi_cost, 2),
        poly_cost_cents=round(poly_cost, 2),
        combined_cost_cents=round(combined, 2),
        spread_cents=round(spread_cents, 2),
        tier=tier,
        hours_to_close=round(hours_to_close, 1),
        detected_at=now,
        kalshi_depth_shares=kalshi_depth,
        poly_depth_shares=poly_depth,
        poly_ask_levels=poly_levels,
    )


def _classify_tier(spread_cents: float) -> str | None:
    """Map spread in cents to tier name. Returns None if below minimum threshold."""
    for name, min_s, max_s in PROFIT_TIERS:
        if min_s <= spread_cents < max_s:
            return name
    # Check Ultra High (unbounded max)
    if spread_cents >= PROFIT_TIERS[0][1]:
        return PROFIT_TIERS[0][0]
    return None


def _combined_str(cost_a: float | None, cost_b: float | None, label: str) -> str:
    """Format a strategy evaluation line for logging."""
    if cost_a is None or cost_b is None:
        return f"  {label} = N/A (missing prices)"
    combined = cost_a + cost_b
    if combined < 100.0:
        spread = round(100.0 - combined, 2)
        tier = _classify_tier(spread)
        tier_str = f", tier={tier}" if tier else ""
        return f"  Strategy {label} = {cost_a:.1f}c + {cost_b:.1f}c = {combined:.1f}c  [ARB: spread={spread:.2f}c{tier_str}]"
    return f"  Strategy {label} = {cost_a:.1f}c + {cost_b:.1f}c = {combined:.1f}c  [NO ARB]"

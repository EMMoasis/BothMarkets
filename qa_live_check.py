"""
Quick live QA script — fetches real markets and shows parsed results + matches.
Run: python qa_live_check.py
"""
import logging
import sys

# Force UTF-8 output on Windows to handle team names with special characters
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

from scanner.kalshi_client import KalshiClient
from scanner.poly_client import PolyClient
from scanner.market_matcher import MarketMatcher
from scanner.opportunity_finder import OpportunityFinder, format_opportunity_log
from scanner.models import MarketType

# --- Kalshi ---
print("\n=== KALSHI MARKETS (72h window, parseable) ===")
k = KalshiClient()
km = k.get_all_markets()

k_crypto = [m for m in km if m.market_type == MarketType.CRYPTO]
k_sports = [m for m in km if m.market_type == MarketType.SPORTS]
print(f"Total Kalshi parseable markets in window: {len(km)} ({len(k_crypto)} crypto, {len(k_sports)} sports)")

def _price_depth(cents, depth) -> str:
    p = f"{cents}c" if cents is not None else "N/A"
    d = f"/{depth:.0f}sh" if depth is not None else ""
    return p + d

if k_crypto:
    print(f"\n  --- Crypto (first 5) ---")
    for m in k_crypto[:5]:
        print(f"  [{m.asset}] {m.direction} ${m.threshold:.0f} | {m.resolution_dt.strftime('%Y-%m-%d %H:%M')} UTC | "
              f"Y-ask={_price_depth(m.yes_ask_cents, m.yes_ask_depth)} "
              f"N-ask={_price_depth(m.no_ask_cents, m.no_ask_depth)} | {m.platform_url}")

if k_sports:
    print(f"\n  --- Sports (first 10) ---")
    for m in k_sports[:10]:
        print(f"  [{m.sport}] {m.team} vs {m.opponent} | {m.resolution_dt.strftime('%Y-%m-%d %H:%M')} UTC | "
              f"YES-ask={_price_depth(m.yes_ask_cents, m.yes_ask_depth)} "
              f"NO-ask={_price_depth(m.no_ask_cents, m.no_ask_depth)} | {m.platform_url}")

# --- Polymarket ---
print(f"\n=== POLYMARKET MARKETS (72h window, parseable) ===")
p = PolyClient()
pm = p.get_all_markets()

p_crypto = [m for m in pm if m.market_type == MarketType.CRYPTO]
p_sports = [m for m in pm if m.market_type == MarketType.SPORTS]
print(f"Total Poly parseable markets in window: {len(pm)} ({len(p_crypto)} crypto, {len(p_sports)} sports team-entries)")

if p_crypto:
    print(f"\n  --- Crypto (first 5) ---")
    for m in p_crypto[:5]:
        print(f"  [{m.asset}] {m.direction} ${m.threshold:.0f} | {m.resolution_dt.strftime('%Y-%m-%d %H:%M')} UTC | "
              f"Y-ask={_price_depth(m.yes_ask_cents, m.yes_ask_depth)} "
              f"N-ask={_price_depth(m.no_ask_cents, m.no_ask_depth)} | {m.platform_url}")

if p_sports:
    print(f"\n  --- Sports team-entries (first 10) ---")
    for m in p_sports[:10]:
        print(f"  [{m.sport}] {m.team} vs {m.opponent} | {m.resolution_dt.strftime('%Y-%m-%d %H:%M')} UTC | "
              f"YES-ask={_price_depth(m.yes_ask_cents, m.yes_ask_depth)} "
              f"NO-ask={_price_depth(m.no_ask_cents, m.no_ask_depth)} | {m.platform_url}")

# --- Matching ---
print(f"\n=== MARKET MATCHING ===")
matcher = MarketMatcher()
pairs = matcher.find_matches(km, pm)
print(f"Matched pairs: {len(pairs)}")

if pairs:
    print(f"\n  --- Matched pairs (first 10) ---")
    for pair in pairs[:10]:
        km_m = pair.kalshi
        pm_m = pair.poly
        if km_m.market_type == MarketType.SPORTS:
            label = f"{km_m.sport} | {km_m.team} vs {km_m.opponent}"
        else:
            label = f"{km_m.asset} {km_m.direction} ${km_m.threshold:.0f}"
        print(f"  {label} | {km_m.resolution_dt.strftime('%Y-%m-%d %H:%M')} UTC")
        print(f"    Kalshi:     {km_m.platform_url}")
        print(f"    Polymarket: {pm_m.platform_url}")
        print(f"    K-YES={_price_depth(km_m.yes_ask_cents, km_m.yes_ask_depth)} "
              f"K-NO={_price_depth(km_m.no_ask_cents, km_m.no_ask_depth)} | "
              f"P-YES={_price_depth(pm_m.yes_ask_cents, pm_m.yes_ask_depth)} "
              f"P-NO={_price_depth(pm_m.no_ask_cents, pm_m.no_ask_depth)}")

# --- Arbitrage ---
print(f"\n=== ARBITRAGE OPPORTUNITIES ===")
finder = OpportunityFinder()
opps = finder.find_opportunities(pairs)
if opps:
    for opp in opps:
        print(format_opportunity_log(opp))
        print()
else:
    print("No arbitrage opportunities found in this scan.")
    if not pairs:
        print("(No matched pairs — check that both platforms have overlapping markets in window)")

"""Shared data models for the BothMarkets scanner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Platform(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class MarketType(str, Enum):
    CRYPTO = "crypto"    # e.g. "Will BTC be above $90k?"
    SPORTS = "sports"    # e.g. "Will Team A win vs Team B?"


@dataclass
class NormalizedMarket:
    """
    Platform-agnostic representation of a binary prediction market.

    Both Kalshi and Polymarket markets are converted to this shape
    before matching. All prices are in CENTS (0-100 float range).

    For CRYPTO markets:
      - asset, direction, threshold are used for matching
      - team, opponent, sport, event_id are None

    For SPORTS markets:
      - team (normalized), opponent (normalized), resolution_dt are used for matching
      - asset = sport code (e.g. "CS2", "NBA"), direction = "WIN", threshold = 0.0
      - yes_ask_cents / yes_bid_cents = price for team to WIN
      - no_ask_cents  / no_bid_cents  = price for team to LOSE (opponent wins)
    """
    # --- Identity ---
    platform: Platform
    platform_id: str        # Kalshi: ticker. Poly: conditionId or per-team synthetic ID
    platform_url: str       # Direct URL to this market on the platform
    raw_question: str       # Original question text, unmodified

    # --- Market type ---
    market_type: MarketType = MarketType.CRYPTO

    # --- Crypto matching fields ---
    asset: str = ""         # "BTC", "ETH", "XRP" — or sport code for sports
    direction: str = ""     # "ABOVE" or "BELOW" (crypto), "WIN" (sports)
    threshold: float = 0.0  # Numeric value: 90000.0 for "$90k" (crypto), 0.0 for sports

    # --- Sports matching fields ---
    team: str = ""          # Normalized team name for matching (lower, spaces stripped)
    opponent: str = ""      # Normalized opponent name
    sport: str = ""         # Sport code: "CS2", "NBA", "MLB", "NHL", etc.
    event_id: str = ""      # Platform event group ID (e.g. Kalshi event_ticker)

    # --- Resolution ---
    resolution_dt: datetime = field(default_factory=lambda: datetime.min)

    # --- Live prices in cents (0-100). None = no orderbook data available ---
    yes_ask_cents: float | None = None   # Cost to buy "YES" (team wins for sports)
    no_ask_cents: float | None = None    # Cost to buy "NO" (opponent wins for sports)
    yes_bid_cents: float | None = None
    no_bid_cents: float | None = None

    # --- Token IDs (Polymarket only, needed for CLOB price fetching) ---
    yes_token_id: str | None = None
    no_token_id: str | None = None

    # --- Metadata ---
    liquidity_usd: float = 0.0
    volume_usd: float = 0.0
    raw_data: dict = field(default_factory=dict)


@dataclass
class MatchedPair:
    """
    A pair of markets (one Kalshi, one Polymarket) confirmed to represent
    the same real-world event by strict matching criteria.

    For CRYPTO: matched on asset + direction + threshold + resolution_dt
    For SPORTS: matched on team + opponent + resolution_dt (within ±1h)

    Arbitrage logic:
      - CRYPTO: K_YES_ask + P_NO_ask < 100  (Strategy A)
                K_NO_ask  + P_YES_ask < 100  (Strategy B)
      - SPORTS: K_YES_ask (team A) + P_opponent_token_ask < 100
                K_NO_ask  (team A) + P_team_token_ask     < 100
    """
    kalshi: NormalizedMarket
    poly: NormalizedMarket


@dataclass
class Opportunity:
    """
    A confirmed cross-platform arbitrage opportunity.

    Strategy A: Buy Kalshi YES + Buy Polymarket NO  (or Poly opposing team token)
    Strategy B: Buy Kalshi NO  + Buy Polymarket YES (or Poly same-team token)

    Combined cost < 100c = guaranteed profit regardless of outcome.
    """
    pair: MatchedPair

    kalshi_side: str            # "YES" or "NO"
    poly_side: str              # "YES" or "NO"

    kalshi_cost_cents: float
    poly_cost_cents: float
    combined_cost_cents: float

    spread_cents: float         # = 100 - combined_cost_cents
    tier: str                   # "Ultra High" / "High" / "Mid" / "Low"

    hours_to_close: float       # Hours until the EARLIER of the two close times
    detected_at: datetime       # UTC timestamp when this opportunity was found

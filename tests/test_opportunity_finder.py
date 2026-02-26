"""Tests for OpportunityFinder arbitrage detection and tier classification — crypto and sports."""

from datetime import datetime, timedelta, timezone

import pytest

from scanner.opportunity_finder import (
    OpportunityFinder,
    _classify_tier,
    _evaluate_strategy,
    format_opportunity_log,
)
from scanner.models import MarketType, MatchedPair, NormalizedMarket, Opportunity, Platform


# --- Fixtures ---

def _dt(hours: float = 48.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _make_crypto_pair(
    k_yes_ask=57.0,
    k_no_ask=45.0,
    p_yes_ask=42.0,
    p_no_ask=60.0,
    hours=48.0,
) -> MatchedPair:
    km = NormalizedMarket(
        platform=Platform.KALSHI,
        platform_id="KXBTC-TEST",
        platform_url="https://kalshi.com/markets/KXBTC-TEST",
        raw_question="Will BTC be above $90,000?",
        market_type=MarketType.CRYPTO,
        asset="BTC",
        direction="ABOVE",
        threshold=90000.0,
        resolution_dt=_dt(hours),
        yes_ask_cents=k_yes_ask,
        no_ask_cents=k_no_ask,
        yes_bid_cents=55.0,
        no_bid_cents=43.0,
    )
    pm = NormalizedMarket(
        platform=Platform.POLYMARKET,
        platform_id="0xABC_TEST",
        platform_url="https://polymarket.com/event/btc-above-90k",
        raw_question="Bitcoin above $90k?",
        market_type=MarketType.CRYPTO,
        asset="BTC",
        direction="ABOVE",
        threshold=90000.0,
        resolution_dt=_dt(hours - 0.5),
        yes_ask_cents=p_yes_ask,
        no_ask_cents=p_no_ask,
        yes_bid_cents=40.0,
        no_bid_cents=58.0,
    )
    return MatchedPair(kalshi=km, poly=pm)


def _make_sports_pair(
    k_yes_ask=88.0,  # Kalshi YES: M80 wins
    k_no_ask=20.0,   # Kalshi NO: M80 loses (Voca wins)
    p_yes_ask=73.5,  # Poly YES: M80 wins token
    p_no_ask=26.5,   # Poly NO: Voca wins token
    hours=10.0,
) -> MatchedPair:
    km = NormalizedMarket(
        platform=Platform.KALSHI,
        platform_id="KXCS2GAME-26FEB22M80VOC-M80",
        platform_url="https://kalshi.com/markets/KXCS2GAME-26FEB22M80VOC-M80",
        raw_question="Will M80 win the M80 vs. Voca CS2 match?",
        market_type=MarketType.SPORTS,
        asset="CS2",
        direction="WIN",
        threshold=0.0,
        team="m80",
        opponent="voca",
        sport="CS2",
        resolution_dt=_dt(hours),
        yes_ask_cents=k_yes_ask,
        no_ask_cents=k_no_ask,
        yes_bid_cents=85.0,
        no_bid_cents=18.0,
    )
    pm = NormalizedMarket(
        platform=Platform.POLYMARKET,
        platform_id="0xSPORTS1_m80",
        platform_url="https://polymarket.com/event/cs2-m80-voca",
        raw_question="Counter-Strike: M80 vs Voca (BO3)",
        market_type=MarketType.SPORTS,
        asset="CS2",
        direction="WIN",
        threshold=0.0,
        team="m80",
        opponent="voca",
        sport="CS2",
        resolution_dt=_dt(hours - 0.1),
        yes_ask_cents=p_yes_ask,
        no_ask_cents=p_no_ask,
        yes_bid_cents=72.0,
        no_bid_cents=25.0,
        yes_token_id="TOKEN_M80",
        no_token_id="TOKEN_VOCA",
    )
    return MatchedPair(kalshi=km, poly=pm)


# --- _classify_tier ---

class TestClassifyTier:
    def test_ultra_high(self):
        assert _classify_tier(10.0) == "Ultra High"

    def test_ultra_high_boundary(self):
        assert _classify_tier(9.0) == "Ultra High"

    def test_high(self):
        assert _classify_tier(7.0) == "High"

    def test_high_lower_boundary(self):
        assert _classify_tier(6.0) == "High"

    def test_mid(self):
        assert _classify_tier(5.5) == "Mid"

    def test_mid_lower_boundary(self):
        assert _classify_tier(5.0) == "Mid"

    def test_low(self):
        assert _classify_tier(4.5) == "Low"

    def test_low_lower_boundary(self):
        assert _classify_tier(4.3) == "Low"

    def test_below_threshold_returns_none(self):
        assert _classify_tier(4.2) is None
        assert _classify_tier(0.0) is None
        assert _classify_tier(2.8) is None


# --- _evaluate_strategy ---

class TestEvaluateStrategy:
    def test_profitable_strategy(self):
        pair = _make_crypto_pair(k_yes_ask=51.0, p_no_ask=40.0)
        # 51 + 40 = 91c → spread = 9c → "Ultra High" tier (≥9.0c)
        opp = _evaluate_strategy(pair, 51.0, 40.0, "YES", "NO")
        assert opp is not None
        assert opp.combined_cost_cents == 91.0
        assert opp.spread_cents == 9.0
        assert opp.tier == "Ultra High"
        assert opp.kalshi_side == "YES"
        assert opp.poly_side == "NO"

    def test_combined_exactly_100_not_profitable(self):
        pair = _make_crypto_pair()
        opp = _evaluate_strategy(pair, 60.0, 40.0, "YES", "NO")
        assert opp is None

    def test_combined_above_100_not_profitable(self):
        pair = _make_crypto_pair()
        opp = _evaluate_strategy(pair, 65.0, 40.0, "YES", "NO")
        assert opp is None

    def test_missing_kalshi_cost_skipped(self):
        pair = _make_crypto_pair()
        opp = _evaluate_strategy(pair, None, 40.0, "YES", "NO")
        assert opp is None

    def test_missing_poly_cost_skipped(self):
        pair = _make_crypto_pair()
        opp = _evaluate_strategy(pair, 57.0, None, "YES", "NO")
        assert opp is None

    def test_spread_below_min_skipped(self):
        # 97c combined → 3.0c spread < 4.3c min
        pair = _make_crypto_pair()
        opp = _evaluate_strategy(pair, 57.0, 40.0, "YES", "NO")
        assert opp is None

    def test_hours_to_close_uses_earlier_time(self):
        pair = _make_crypto_pair(hours=48.0)
        opp = _evaluate_strategy(pair, 50.0, 45.0, "YES", "NO")  # 50+45=95 → 5c spread ≥ 4.3c min
        assert opp is not None
        assert 47.0 <= opp.hours_to_close <= 48.0

    def test_sports_strategy_profitable(self):
        pair = _make_sports_pair(k_yes_ask=88.0, p_no_ask=5.0)  # 88+5=93 → spread=7
        opp = _evaluate_strategy(pair, 88.0, 5.0, "YES", "NO")
        assert opp is not None
        assert opp.spread_cents == 7.0


# --- OpportunityFinder.find_opportunities ---

class TestOpportunityFinder:
    def setup_method(self):
        self.finder = OpportunityFinder()

    def test_crypto_strategy_a_found(self):
        # K-YES=51, P-NO=40 → 91c → spread 9c ≥ 4.3c min
        pair = _make_crypto_pair(k_yes_ask=51.0, p_no_ask=40.0)
        opps = self.finder.find_opportunities([pair])
        strat_a = [o for o in opps if o.kalshi_side == "YES" and o.poly_side == "NO"]
        assert len(strat_a) == 1
        assert strat_a[0].spread_cents == 9.0

    def test_crypto_strategy_b_found(self):
        # K-NO=45, P-YES=42 → 87c → spread 13c
        pair = _make_crypto_pair(k_no_ask=45.0, p_yes_ask=42.0)
        opps = self.finder.find_opportunities([pair])
        strat_b = [o for o in opps if o.kalshi_side == "NO" and o.poly_side == "YES"]
        assert len(strat_b) == 1
        assert strat_b[0].spread_cents == 13.0

    def test_sports_opportunity_found(self):
        # K-YES=88 (M80 wins on Kalshi) + P-NO=5 (Voca wins on Poly) = 93 → 7c spread
        pair = _make_sports_pair(k_yes_ask=88.0, p_no_ask=5.0)
        opps = self.finder.find_opportunities([pair])
        strat_a = [o for o in opps if o.kalshi_side == "YES" and o.poly_side == "NO"]
        assert len(strat_a) == 1
        assert strat_a[0].spread_cents == 7.0

    def test_both_strategies_found(self):
        # A: 51+40=91c (9c ≥ 4.3c min), B: 45+42=87c (13c) — both profitable
        pair = _make_crypto_pair(k_yes_ask=51.0, k_no_ask=45.0, p_yes_ask=42.0, p_no_ask=40.0)
        opps = self.finder.find_opportunities([pair])
        assert len(opps) == 2

    def test_no_opportunities_when_combined_100(self):
        pair = _make_crypto_pair(k_yes_ask=60.0, p_no_ask=40.0, k_no_ask=65.0, p_yes_ask=55.0)
        opps = self.finder.find_opportunities([pair])
        strat_a = [o for o in opps if o.kalshi_side == "YES" and o.poly_side == "NO"]
        assert len(strat_a) == 0

    def test_sorted_best_spread_first(self):
        pair1 = _make_crypto_pair(k_no_ask=45.0, p_yes_ask=42.0)    # B: 87c → spread=13c
        pair2 = _make_crypto_pair(k_yes_ask=51.0, p_no_ask=40.0)    # A: 91c → spread=9c
        opps = self.finder.find_opportunities([pair1, pair2])
        assert opps[0].spread_cents >= opps[-1].spread_cents

    def test_empty_pairs_returns_empty(self):
        assert self.finder.find_opportunities([]) == []

    def test_multiple_pairs(self):
        pair1 = _make_crypto_pair(k_yes_ask=50.0, p_no_ask=40.0)    # A: 90c → 10c
        pair2 = _make_crypto_pair(k_no_ask=48.0, p_yes_ask=45.0)    # B: 93c → 7c
        opps = self.finder.find_opportunities([pair1, pair2])
        assert len(opps) >= 2


# --- format_opportunity_log (crypto) ---

class TestFormatOpportunityLogCrypto:
    def test_contains_key_fields(self):
        pair = _make_crypto_pair(k_yes_ask=51.0, p_no_ask=40.0)  # 91c → 9c spread ≥ 4.3c min
        opp = _evaluate_strategy(pair, 51.0, 40.0, "YES", "NO")
        assert opp is not None
        text = format_opportunity_log(opp)
        assert "ARB OPPORTUNITY" in text
        assert "BTC" in text
        assert "90000" in text
        assert "kalshi.com/markets/" in text
        assert "polymarket.com/event/" in text
        assert "51.0c" in text
        assert "40.0c" in text
        assert "91.0c" in text

    def test_crypto_strategy_label(self):
        pair = _make_crypto_pair(k_yes_ask=51.0, p_no_ask=40.0)  # 91c → 9c spread ≥ 4.3c min
        opp = _evaluate_strategy(pair, 51.0, 40.0, "YES", "NO")
        text = format_opportunity_log(opp)
        # Should say "Kalshi YES + Polymarket NO" for crypto
        assert "Kalshi YES" in text
        assert "Polymarket NO" in text


# --- format_opportunity_log (sports) ---

class TestFormatOpportunityLogSports:
    def test_sports_contains_team_names(self):
        pair = _make_sports_pair(k_yes_ask=88.0, p_no_ask=5.0)
        opp = _evaluate_strategy(pair, 88.0, 5.0, "YES", "NO")
        assert opp is not None
        text = format_opportunity_log(opp)
        assert "ARB OPPORTUNITY" in text
        assert "CS2" in text
        assert "m80" in text
        assert "voca" in text
        assert "kalshi.com/markets/" in text
        assert "polymarket.com/event/" in text

    def test_sports_strategy_label_yes(self):
        pair = _make_sports_pair(k_yes_ask=88.0, p_no_ask=5.0)
        opp = _evaluate_strategy(pair, 88.0, 5.0, "YES", "NO")
        text = format_opportunity_log(opp)
        # Should clarify team wins, not just YES/NO
        assert "wins" in text.lower()

    def test_sports_strategy_label_no(self):
        pair = _make_sports_pair(k_no_ask=20.0, p_yes_ask=73.5)
        opp = _evaluate_strategy(pair, 20.0, 73.5, "NO", "YES")
        # 20 + 73.5 = 93.5 → 6.5c spread → Ultra High
        assert opp is not None
        text = format_opportunity_log(opp)
        assert "loses" in text.lower() or "NO" in text

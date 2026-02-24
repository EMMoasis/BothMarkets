"""Tests for strict market matching — crypto and sports."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from scanner.market_matcher import (
    MarketMatcher,
    _check_crypto_match,
    _check_match,
    _check_sports_match,
)
from scanner.models import MarketType, MatchedPair, NormalizedMarket, Platform


# --- Fixtures ---

def _dt(hours_from_now: float = 48.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours_from_now)


def _kalshi_crypto(
    ticker="KXBTC-26FEB21-T90000",
    asset="BTC",
    direction="ABOVE",
    threshold=90000.0,
    hours=48.0,
) -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_id=ticker,
        platform_url=f"https://kalshi.com/markets/{ticker}",
        raw_question=f"Will {asset} be {direction.lower()} ${threshold:.0f}?",
        market_type=MarketType.CRYPTO,
        asset=asset,
        direction=direction,
        threshold=threshold,
        resolution_dt=_dt(hours),
        yes_ask_cents=57.0,
        no_ask_cents=45.0,
        yes_bid_cents=55.0,
        no_bid_cents=43.0,
    )


def _poly_crypto(
    condition_id="0xPOLY1",
    asset="BTC",
    direction="ABOVE",
    threshold=90000.0,
    hours=48.0,
    slug="btc-above-90k",
) -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.POLYMARKET,
        platform_id=condition_id,
        platform_url=f"https://polymarket.com/event/{slug}",
        raw_question=f"Bitcoin {direction.lower()} ${threshold:.0f}?",
        market_type=MarketType.CRYPTO,
        asset=asset,
        direction=direction,
        threshold=threshold,
        resolution_dt=_dt(hours),
        yes_ask_cents=42.0,
        no_ask_cents=60.0,
        yes_bid_cents=40.0,
        no_bid_cents=58.0,
    )


def _kalshi_sports(
    ticker="KXCS2GAME-26FEB22M80VOC-M80",
    sport="CS2",
    team="m80",
    opponent="voca",
    hours=10.0,
    event_id="KXCS2GAME-26FEB22M80VOC",
) -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_id=ticker,
        platform_url=f"https://kalshi.com/markets/{ticker}",
        raw_question=f"Will {team} win the {team} vs. {opponent} CS2 match?",
        market_type=MarketType.SPORTS,
        asset=sport,
        direction="WIN",
        threshold=0.0,
        team=team,
        opponent=opponent,
        sport=sport,
        event_id=event_id,
        resolution_dt=_dt(hours),
        yes_ask_cents=88.0,
        no_ask_cents=45.0,
        yes_bid_cents=85.0,
        no_bid_cents=42.0,
    )


def _poly_sports(
    condition_id="0xSPORTS1",
    sport="CS2",
    team="m80",
    opponent="voca",
    hours=10.0,
    slug="cs2-m80-voca-match",
) -> NormalizedMarket:
    synthetic_id = f"{condition_id}_{team}"
    return NormalizedMarket(
        platform=Platform.POLYMARKET,
        platform_id=synthetic_id,
        platform_url=f"https://polymarket.com/event/{slug}",
        raw_question="Counter-Strike: M80 vs Voca (BO3)",
        market_type=MarketType.SPORTS,
        asset=sport,
        direction="WIN",
        threshold=0.0,
        team=team,
        opponent=opponent,
        sport=sport,
        event_id=condition_id,
        resolution_dt=_dt(hours),
        yes_ask_cents=73.5,
        no_ask_cents=26.5,
        yes_bid_cents=72.0,
        no_bid_cents=25.0,
        yes_token_id="token_m80",
        no_token_id="token_voca",
    )


# --- _check_crypto_match ---

class TestCheckCryptoMatch:
    def test_all_criteria_match_returns_none(self):
        km = _kalshi_crypto()
        pm = _poly_crypto()
        assert _check_crypto_match(km, pm) is None

    def test_different_asset_fails(self):
        km = _kalshi_crypto(asset="BTC")
        pm = _poly_crypto(asset="ETH")
        assert _check_crypto_match(km, pm) == "asset"

    def test_different_direction_fails(self):
        km = _kalshi_crypto(direction="ABOVE")
        pm = _poly_crypto(direction="BELOW")
        assert _check_crypto_match(km, pm) == "direction"

    def test_date_too_far_apart_fails(self):
        km = _kalshi_crypto(hours=48.0)
        pm = _poly_crypto(hours=56.0)  # 8h apart
        assert _check_crypto_match(km, pm) == "date"

    def test_date_within_tolerance_passes(self):
        km = _kalshi_crypto(hours=48.0)
        pm = _poly_crypto(hours=48.75)  # 45 minutes
        assert _check_crypto_match(km, pm) is None

    def test_different_threshold_fails(self):
        km = _kalshi_crypto(threshold=90000.0)
        pm = _poly_crypto(threshold=95000.0)
        assert _check_crypto_match(km, pm) == "threshold"


# --- _check_sports_match ---

class TestCheckSportsMatch:
    def test_all_criteria_match_returns_none(self):
        km = _kalshi_sports()
        pm = _poly_sports()
        assert _check_sports_match(km, pm) is None

    def test_different_sport_fails(self):
        km = _kalshi_sports(sport="CS2")
        pm = _poly_sports(sport="NBA")
        assert _check_sports_match(km, pm) == "sport"

    def test_different_team_fails(self):
        km = _kalshi_sports(team="m80")
        pm = _poly_sports(team="fnatic")
        assert _check_sports_match(km, pm) == "team"

    def test_date_too_far_fails(self):
        km = _kalshi_sports(hours=10.0)
        pm = _poly_sports(hours=20.0)  # 10h apart
        assert _check_sports_match(km, pm) == "date"

    def test_date_within_tolerance_passes(self):
        km = _kalshi_sports(hours=10.0)
        pm = _poly_sports(hours=10.5)  # 30 minutes
        assert _check_sports_match(km, pm) is None

    def test_different_map_number_fails(self):
        km = _kalshi_sports()
        km.map_number = 1
        pm = _poly_sports()
        pm.map_number = 2
        assert _check_sports_match(km, pm) == "map_number"

    def test_same_map_number_passes(self):
        km = _kalshi_sports()
        km.map_number = 2
        pm = _poly_sports()
        pm.map_number = 2
        assert _check_sports_match(km, pm) is None

    def test_one_map_number_none_skips_check(self):
        # If either has None map_number, skip the check (backward compat)
        km = _kalshi_sports()
        km.map_number = 1
        pm = _poly_sports()
        pm.map_number = None
        assert _check_sports_match(km, pm) is None

    # --- Opponent check ---

    def test_different_opponent_fails(self):
        """DRX vs TeamA should not match DRX vs TeamB — same team, different game."""
        km = _kalshi_sports(team="m80", opponent="voca")
        pm = _poly_sports(team="m80", opponent="fnatic")   # different opponent
        assert _check_sports_match(km, pm) == "opponent"

    def test_same_opponent_passes(self):
        km = _kalshi_sports(team="m80", opponent="voca")
        pm = _poly_sports(team="m80", opponent="voca")
        assert _check_sports_match(km, pm) is None

    def test_opponent_check_skipped_when_kalshi_opponent_empty(self):
        """If Kalshi has no opponent (shouldn't happen but defensive), skip check."""
        km = _kalshi_sports(team="m80", opponent="")
        pm = _poly_sports(team="m80", opponent="fnatic")
        assert _check_sports_match(km, pm) is None

    def test_opponent_check_skipped_when_poly_opponent_empty(self):
        km = _kalshi_sports(team="m80", opponent="voca")
        pm = _poly_sports(team="m80", opponent="")
        assert _check_sports_match(km, pm) is None


# --- _check_match (dispatcher) ---

class TestCheckMatch:
    def test_dispatches_crypto(self):
        km = _kalshi_crypto()
        pm = _poly_crypto()
        assert _check_match(km, pm) is None

    def test_dispatches_sports(self):
        km = _kalshi_sports()
        pm = _poly_sports()
        assert _check_match(km, pm) is None

    def test_dispatches_sports_failure(self):
        km = _kalshi_sports(team="m80")
        pm = _poly_sports(team="fnatic")
        assert _check_match(km, pm) == "team"


# --- MarketMatcher.find_matches (crypto) ---

_CRYPTO_ENABLED = "scanner.market_matcher.CRYPTO_MATCHING_ENABLED"


class TestMarketMatcherCrypto:
    """
    Crypto matching is disabled by default (different oracles: BRTI vs Binance).
    Tests that exercise the matching logic patch CRYPTO_MATCHING_ENABLED=True.
    """
    def setup_method(self):
        self.matcher = MarketMatcher()

    def test_disabled_by_default_returns_no_crypto_pairs(self):
        """With CRYPTO_MATCHING_ENABLED=False (default) crypto pairs are never produced."""
        km = _kalshi_crypto()
        pm = _poly_crypto()
        pairs = self.matcher.find_matches([km], [pm])
        assert len(pairs) == 0

    def test_perfect_match_when_enabled(self):
        km = _kalshi_crypto()
        pm = _poly_crypto()
        with patch(_CRYPTO_ENABLED, True):
            pairs = self.matcher.find_matches([km], [pm])
        assert len(pairs) == 1
        assert pairs[0].kalshi.platform_id == "KXBTC-26FEB21-T90000"
        assert pairs[0].poly.platform_id == "0xPOLY1"

    def test_no_match_different_asset(self):
        km = _kalshi_crypto(asset="BTC")
        pm = _poly_crypto(asset="ETH")
        with patch(_CRYPTO_ENABLED, True):
            assert self.matcher.find_matches([km], [pm]) == []

    def test_no_match_different_threshold(self):
        km = _kalshi_crypto(threshold=90000.0)
        pm = _poly_crypto(threshold=85000.0)
        with patch(_CRYPTO_ENABLED, True):
            assert self.matcher.find_matches([km], [pm]) == []

    def test_multiple_crypto_matches_when_enabled(self):
        km1 = _kalshi_crypto(ticker="K1", asset="BTC", threshold=90000.0)
        km2 = _kalshi_crypto(ticker="K2", asset="ETH", threshold=3000.0)
        pm1 = _poly_crypto(condition_id="P1", asset="BTC", threshold=90000.0)
        pm2 = _poly_crypto(condition_id="P2", asset="ETH", threshold=3000.0)
        with patch(_CRYPTO_ENABLED, True):
            pairs = self.matcher.find_matches([km1, km2], [pm1, pm2])
        assert len(pairs) == 2

    def test_dedup_kalshi_matches_only_once(self):
        km = _kalshi_crypto(ticker="K1")
        pm1 = _poly_crypto(condition_id="P1")
        pm2 = _poly_crypto(condition_id="P2")
        with patch(_CRYPTO_ENABLED, True):
            pairs = self.matcher.find_matches([km], [pm1, pm2])
        assert len(pairs) == 1

    def test_empty_returns_empty(self):
        with patch(_CRYPTO_ENABLED, True):
            assert self.matcher.find_matches([], [_poly_crypto()]) == []
            assert self.matcher.find_matches([_kalshi_crypto()], []) == []

    def test_logs_urls_when_enabled(self, caplog):
        import logging
        km = _kalshi_crypto()
        pm = _poly_crypto()
        with caplog.at_level(logging.INFO, logger="scanner.market_matcher"):
            with patch(_CRYPTO_ENABLED, True):
                self.matcher.find_matches([km], [pm])
        assert "kalshi.com/markets/" in caplog.text
        assert "polymarket.com/event/" in caplog.text


# --- MarketMatcher.find_matches (sports) ---

class TestMarketMatcherSports:
    def setup_method(self):
        self.matcher = MarketMatcher()

    def test_sports_match(self):
        km = _kalshi_sports()
        pm = _poly_sports()
        pairs = self.matcher.find_matches([km], [pm])
        assert len(pairs) == 1
        assert pairs[0].kalshi.team == "m80"
        assert pairs[0].poly.team == "m80"

    def test_no_sports_match_different_team(self):
        km = _kalshi_sports(team="m80")
        pm = _poly_sports(team="fnatic")
        pairs = self.matcher.find_matches([km], [pm])
        assert len(pairs) == 0

    def test_no_sports_match_different_sport(self):
        km = _kalshi_sports(sport="CS2")
        pm = _poly_sports(sport="NBA")
        pairs = self.matcher.find_matches([km], [pm])
        assert len(pairs) == 0

    def test_sports_match_logs_urls(self, caplog):
        import logging
        km = _kalshi_sports()
        pm = _poly_sports()
        with caplog.at_level(logging.INFO, logger="scanner.market_matcher"):
            self.matcher.find_matches([km], [pm])
        assert "kalshi.com/markets/" in caplog.text
        assert "polymarket.com/event/" in caplog.text
        assert "SPORTS" in caplog.text

    def test_multiple_sports_matches(self):
        km1 = _kalshi_sports(ticker="K1", team="m80", opponent="voca")
        km2 = _kalshi_sports(ticker="K2", team="fnatic", opponent="vitality")
        pm1 = _poly_sports(condition_id="P1", team="m80", opponent="voca")
        pm2 = _poly_sports(condition_id="P2", team="fnatic", opponent="vitality")
        pairs = self.matcher.find_matches([km1, km2], [pm1, pm2])
        assert len(pairs) == 2


# --- Mixed crypto + sports ---

class TestMixedMarkets:
    def setup_method(self):
        self.matcher = MarketMatcher()

    def test_crypto_and_sports_matched_separately_when_crypto_enabled(self):
        """When CRYPTO_MATCHING_ENABLED=True, both crypto and sports pairs are found."""
        km_c = _kalshi_crypto()
        km_s = _kalshi_sports()
        pm_c = _poly_crypto()
        pm_s = _poly_sports()
        with patch(_CRYPTO_ENABLED, True):
            pairs = self.matcher.find_matches([km_c, km_s], [pm_c, pm_s])
        assert len(pairs) == 2
        types = {p.kalshi.market_type for p in pairs}
        assert MarketType.CRYPTO in types
        assert MarketType.SPORTS in types

    def test_crypto_disabled_sports_still_match(self):
        """With CRYPTO_MATCHING_ENABLED=False (default), only sports pairs are found."""
        km_c = _kalshi_crypto()
        km_s = _kalshi_sports()
        pm_c = _poly_crypto()
        pm_s = _poly_sports()
        pairs = self.matcher.find_matches([km_c, km_s], [pm_c, pm_s])
        assert len(pairs) == 1
        assert pairs[0].kalshi.market_type == MarketType.SPORTS

    def test_crypto_does_not_match_sports(self):
        # Kalshi crypto vs Poly sports — should produce 0 matches
        km_c = _kalshi_crypto()
        pm_s = _poly_sports()
        pairs = self.matcher.find_matches([km_c], [pm_s])
        assert len(pairs) == 0

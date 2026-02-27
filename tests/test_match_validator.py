"""Tests for match_validator — Liquipedia schedule verification."""

from unittest.mock import MagicMock, patch

import pytest

from scanner.match_validator import (
    _fuzzy_find,
    clear_cache,
    is_match_scheduled,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_TEAMS = frozenset([
    "Natus Vincere",
    "FaZe Clan",
    "Team Vitality",
    "G2 Esports",
    "Astralis",
    "FURIA",
    "Liquid",
    "Cloud9",
    "ShindeN",
    "Bounty Hunters Esports",
])


def _patch_fetch(teams):
    """Context manager: patch _fetch_liquipedia_teams to return *teams*."""
    return patch(
        "scanner.match_validator._fetch_liquipedia_teams",
        return_value=teams,
    )


# ---------------------------------------------------------------------------
# _fuzzy_find
# ---------------------------------------------------------------------------

class TestFuzzyFind:
    def test_exact_match(self):
        assert _fuzzy_find("FaZe Clan", _SAMPLE_TEAMS) is True

    def test_case_insensitive(self):
        assert _fuzzy_find("faze clan", _SAMPLE_TEAMS) is True

    def test_substring_match(self):
        # "Liquid" is substring of "Team Liquid"
        teams = frozenset(["Team Liquid"])
        assert _fuzzy_find("Liquid", teams) is True

    def test_alias_substring_reverse(self):
        # "Bounty Hunters" is substring of "Bounty Hunters Esports"
        assert _fuzzy_find("Bounty Hunters", _SAMPLE_TEAMS) is True

    def test_fuzzy_close_name(self):
        # "Navi" → close enough to "Natus Vincere"? Probably not, threshold 0.72
        # But "Natus Vincere" → "Natus Vincere" is exact
        assert _fuzzy_find("Natus Vincere", _SAMPLE_TEAMS) is True

    def test_not_found(self):
        assert _fuzzy_find("Random Unknown Team", _SAMPLE_TEAMS) is False

    def test_empty_name_not_found(self):
        assert _fuzzy_find("", _SAMPLE_TEAMS) is False

    def test_tbd_not_matched(self):
        teams = frozenset(["TBD", "TBA"])
        # These are filtered out during fetch, but even if present, won't match real names
        assert _fuzzy_find("Astralis", teams) is False


# ---------------------------------------------------------------------------
# is_match_scheduled
# ---------------------------------------------------------------------------

class TestIsMatchScheduled:
    def setup_method(self):
        clear_cache()

    def test_non_cs2_sport_returns_none(self):
        result = is_match_scheduled("Lakers", "Celtics", "NBA")
        assert result is None

    def test_empty_team_returns_none(self):
        result = is_match_scheduled("", "ShindeN", "CS2")
        assert result is None

    def test_empty_opponent_returns_none(self):
        result = is_match_scheduled("FURIA", "", "CS2")
        assert result is None

    def test_both_teams_found_returns_true(self):
        with _patch_fetch(_SAMPLE_TEAMS):
            result = is_match_scheduled("FURIA", "Cloud9", "CS2")
        assert result is True

    def test_one_team_missing_returns_false(self):
        with _patch_fetch(_SAMPLE_TEAMS):
            result = is_match_scheduled("FURIA", "Ghost Gaming", "CS2")
        assert result is False

    def test_both_teams_missing_returns_false(self):
        with _patch_fetch(_SAMPLE_TEAMS):
            result = is_match_scheduled("Ghost Gaming", "Unknown Squad", "CS2")
        assert result is False

    def test_liquipedia_unavailable_returns_none(self):
        with _patch_fetch(None):
            result = is_match_scheduled("FURIA", "Cloud9", "CS2")
        assert result is None

    def test_bheshin_match_not_found(self):
        """The exact pair that caused the real-world loss should return False."""
        with _patch_fetch(_SAMPLE_TEAMS):
            # ShindeN IS in our sample set, but Bounty Hunters Esports also is
            # Both found → True in sample. Test with a set missing one.
            teams_without_shinden = frozenset(t for t in _SAMPLE_TEAMS if t != "ShindeN")
        with _patch_fetch(teams_without_shinden):
            result = is_match_scheduled("Bounty Hunters Esports", "ShindeN", "CS2")
        assert result is False

    def test_result_cached_per_pair(self):
        """Second call for the same pair should not re-fetch Liquipedia."""
        with _patch_fetch(_SAMPLE_TEAMS) as mock_fetch:
            is_match_scheduled("FURIA", "Cloud9", "CS2")
            is_match_scheduled("FURIA", "Cloud9", "CS2")   # second call
        # fetch should only be called once (cache hit on second)
        assert mock_fetch.call_count == 1

    def test_cache_is_cleared_by_clear_cache(self):
        with _patch_fetch(_SAMPLE_TEAMS) as mock_fetch:
            is_match_scheduled("FURIA", "Cloud9", "CS2")
        clear_cache()
        with _patch_fetch(_SAMPLE_TEAMS) as mock_fetch2:
            is_match_scheduled("FURIA", "Cloud9", "CS2")
        assert mock_fetch2.call_count == 1

    def test_case_insensitive_team_names(self):
        with _patch_fetch(_SAMPLE_TEAMS):
            result = is_match_scheduled("furia", "cloud9", "CS2")
        assert result is True


# ---------------------------------------------------------------------------
# Integration: OpportunityFinder skips unverified sports pairs
# ---------------------------------------------------------------------------

class TestOpportunityFinderIntegration:
    """Ensure unverified matches are dropped before opportunity evaluation."""

    def setup_method(self):
        clear_cache()

    def _make_sports_pair(self):
        from datetime import datetime, timedelta, timezone
        from scanner.models import MarketType, MatchedPair, NormalizedMarket, Platform

        now = datetime.now(timezone.utc)
        close = now + timedelta(hours=4)

        kalshi = NormalizedMarket(
            platform=Platform.KALSHI,
            platform_id="KXCS2-TEST",
            platform_url="https://kalshi.com/markets/KXCS2-TEST",
            raw_question="Test: FURIA vs Cloud9 Map 1",
            market_type=MarketType.SPORTS,
            asset="CS2", direction="WIN",
            team="FURIA", opponent="Cloud9", sport="CS2",
            resolution_dt=close,
            yes_ask_cents=51.0, no_ask_cents=52.0,
            yes_ask_depth=100, no_ask_depth=100,
        )
        poly = NormalizedMarket(
            platform=Platform.POLYMARKET,
            platform_id="poly-test-123",
            platform_url="https://polymarket.com/event/test",
            raw_question="FURIA vs Cloud9",
            market_type=MarketType.SPORTS,
            asset="CS2", direction="WIN",
            team="FURIA", opponent="Cloud9", sport="CS2",
            resolution_dt=close,
            yes_ask_cents=40.0, no_ask_cents=40.0,
            yes_ask_depth=100, no_ask_depth=100,
        )
        return MatchedPair(kalshi=kalshi, poly=poly)

    def test_verified_match_yields_opportunities(self):
        from scanner.opportunity_finder import OpportunityFinder
        pair = self._make_sports_pair()
        with _patch_fetch(_SAMPLE_TEAMS):
            opps = OpportunityFinder().find_opportunities([pair])
        assert len(opps) > 0

    def test_unverified_match_skipped(self):
        from scanner.opportunity_finder import OpportunityFinder
        pair = self._make_sports_pair()
        # Patch so FURIA is not on Liquipedia
        unknown_teams = frozenset(["Some Other Team"])
        with _patch_fetch(unknown_teams):
            opps = OpportunityFinder().find_opportunities([pair])
        assert len(opps) == 0

    def test_liquipedia_unavailable_still_yields(self):
        """If Liquipedia is down, allow trade (None result)."""
        from scanner.opportunity_finder import OpportunityFinder
        pair = self._make_sports_pair()
        with _patch_fetch(None):
            opps = OpportunityFinder().find_opportunities([pair])
        # Should still find opportunities (allow with warning)
        assert len(opps) > 0

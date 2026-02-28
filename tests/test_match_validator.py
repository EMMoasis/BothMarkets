"""Tests for match_validator — Liquipedia schedule verification."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from scanner.match_validator import (
    SUPPORTED_SPORTS,
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


@contextmanager
def _patch_fetch(teams):
    """Context manager: patch _fetch_liquipedia_teams_api to return *teams*
    and set a fake API key so the key-check passes.
    The mock ignores the wiki argument and always returns the given set.
    """
    with patch("scanner.match_validator._get_api_key", return_value="fake-test-key"), \
         patch("scanner.match_validator._fetch_liquipedia_teams_api", return_value=teams) as mock:
        yield mock


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
        # Exact match case
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
# SUPPORTED_SPORTS export
# ---------------------------------------------------------------------------

class TestSupportedSports:
    def test_cs2_supported(self):
        assert "CS2" in SUPPORTED_SPORTS

    def test_lol_supported(self):
        assert "LOL" in SUPPORTED_SPORTS

    def test_valorant_supported(self):
        assert "VALORANT" in SUPPORTED_SPORTS

    def test_dota2_supported(self):
        assert "DOTA2" in SUPPORTED_SPORTS

    def test_rl_supported(self):
        assert "RL" in SUPPORTED_SPORTS

    def test_nba_not_supported(self):
        assert "NBA" not in SUPPORTED_SPORTS

    def test_nfl_not_supported(self):
        assert "NFL" not in SUPPORTED_SPORTS

    def test_soccer_not_supported(self):
        assert "SOCCER" not in SUPPORTED_SPORTS


# ---------------------------------------------------------------------------
# is_match_scheduled
# ---------------------------------------------------------------------------

class TestIsMatchScheduled:
    def setup_method(self):
        clear_cache()

    # --- Unsupported / traditional sports return None immediately ---

    def test_nba_returns_none(self):
        """Traditional sports with no Liquipedia page return None (allow through)."""
        result = is_match_scheduled("Lakers", "Celtics", "NBA")
        assert result is None

    def test_nfl_returns_none(self):
        result = is_match_scheduled("Chiefs", "Eagles", "NFL")
        assert result is None

    def test_soccer_returns_none(self):
        result = is_match_scheduled("Manchester City", "Arsenal", "SOCCER")
        assert result is None

    def test_empty_team_returns_none(self):
        result = is_match_scheduled("", "ShindeN", "CS2")
        assert result is None

    def test_empty_opponent_returns_none(self):
        result = is_match_scheduled("FURIA", "", "CS2")
        assert result is None

    # --- CS2 validation ---

    def test_cs2_both_teams_found_returns_true(self):
        with _patch_fetch(_SAMPLE_TEAMS):
            result = is_match_scheduled("FURIA", "Cloud9", "CS2")
        assert result is True

    def test_cs2_one_team_missing_returns_false(self):
        with _patch_fetch(_SAMPLE_TEAMS):
            result = is_match_scheduled("FURIA", "Ghost Gaming", "CS2")
        assert result is False

    def test_cs2_both_teams_missing_returns_false(self):
        with _patch_fetch(_SAMPLE_TEAMS):
            result = is_match_scheduled("Ghost Gaming", "Unknown Squad", "CS2")
        assert result is False

    def test_cs2_liquipedia_unavailable_returns_none(self):
        with _patch_fetch(None):
            result = is_match_scheduled("FURIA", "Cloud9", "CS2")
        assert result is None

    def test_cs2_case_insensitive_team_names(self):
        with _patch_fetch(_SAMPLE_TEAMS):
            result = is_match_scheduled("furia", "cloud9", "CS2")
        assert result is True

    # --- LOL validation (same logic, different Liquipedia URL) ---

    def test_lol_both_teams_found_returns_true(self):
        lol_teams = frozenset(["T1", "Gen.G", "Team Liquid", "Lyon Esports"])
        with _patch_fetch(lol_teams):
            result = is_match_scheduled("T1", "Gen.G", "LOL")
        assert result is True

    def test_lol_team_not_found_returns_false(self):
        lol_teams = frozenset(["T1", "Gen.G"])
        with _patch_fetch(lol_teams):
            result = is_match_scheduled("T1", "UnknownTeam", "LOL")
        assert result is False

    def test_lol_liquipedia_unavailable_returns_none(self):
        with _patch_fetch(None):
            result = is_match_scheduled("T1", "Gen.G", "LOL")
        assert result is None

    def test_lol_lyon_vs_liquid_found(self):
        """Real-world pair that was previously getting false 'unavailable' warnings."""
        lol_teams = frozenset(["Lyon Esports", "Team Liquid"])
        with _patch_fetch(lol_teams):
            result = is_match_scheduled("lyon", "liquid", "LOL")
        assert result is True

    # --- VALORANT validation ---

    def test_valorant_both_teams_found_returns_true(self):
        val_teams = frozenset(["Novo Esports", "Falke Esports", "Sentinels"])
        with _patch_fetch(val_teams):
            result = is_match_scheduled("Novo Esports", "Falke Esports", "VALORANT")
        assert result is True

    def test_valorant_team_not_found_returns_false(self):
        val_teams = frozenset(["Sentinels", "NRG"])
        with _patch_fetch(val_teams):
            result = is_match_scheduled("Novo Esports", "Falke Esports", "VALORANT")
        assert result is False

    # --- DOTA2 validation ---

    def test_dota2_both_teams_found_returns_true(self):
        dota_teams = frozenset(["Team Spirit", "OG", "Tundra Esports"])
        with _patch_fetch(dota_teams):
            result = is_match_scheduled("Team Spirit", "OG", "DOTA2")
        assert result is True

    # --- Rocket League validation ---

    def test_rl_both_teams_found_returns_true(self):
        rl_teams = frozenset(["Team Falcons", "G2 Esports", "Vitality"])
        with _patch_fetch(rl_teams):
            result = is_match_scheduled("Team Falcons", "G2 Esports", "RL")
        assert result is True

    # --- Cache behaviour (sport-keyed) ---

    def test_result_cached_per_pair(self):
        """Second call for the same pair should not re-fetch Liquipedia."""
        with _patch_fetch(_SAMPLE_TEAMS) as mock_fetch:
            is_match_scheduled("FURIA", "Cloud9", "CS2")
            is_match_scheduled("FURIA", "Cloud9", "CS2")   # second call
        # fetch should only be called once (cache hit on second)
        assert mock_fetch.call_count == 1

    def test_different_sports_fetch_separately(self):
        """CS2 and LOL caches are keyed independently — both need a fetch."""
        with _patch_fetch(_SAMPLE_TEAMS) as mock_fetch:
            is_match_scheduled("FURIA", "Cloud9", "CS2")
            is_match_scheduled("T1", "Gen.G", "LOL")
        # Two different sport keys → two separate fetches
        assert mock_fetch.call_count == 2

    def test_cache_is_cleared_by_clear_cache(self):
        with _patch_fetch(_SAMPLE_TEAMS):
            is_match_scheduled("FURIA", "Cloud9", "CS2")
        clear_cache()
        with _patch_fetch(_SAMPLE_TEAMS) as mock_fetch2:
            is_match_scheduled("FURIA", "Cloud9", "CS2")
        assert mock_fetch2.call_count == 1

    def test_bheshin_match_not_found(self):
        """The exact pair that caused the real-world loss should return False."""
        teams_without_shinden = frozenset(t for t in _SAMPLE_TEAMS if t != "ShindeN")
        with _patch_fetch(teams_without_shinden):
            result = is_match_scheduled("Bounty Hunters Esports", "ShindeN", "CS2")
        assert result is False


# ---------------------------------------------------------------------------
# Integration: OpportunityFinder skips unverified sports pairs
# ---------------------------------------------------------------------------

class TestOpportunityFinderIntegration:
    """Ensure unverified matches are dropped before opportunity evaluation."""

    def setup_method(self):
        clear_cache()

    def _make_sports_pair(self, sport: str = "CS2", team: str = "FURIA", opponent: str = "Cloud9"):
        from datetime import datetime, timedelta, timezone
        from scanner.models import MarketType, MatchedPair, NormalizedMarket, Platform

        now = datetime.now(timezone.utc)
        close = now + timedelta(hours=4)

        kalshi = NormalizedMarket(
            platform=Platform.KALSHI,
            platform_id=f"KX{sport}-TEST",
            platform_url=f"https://kalshi.com/markets/KX{sport}-TEST",
            raw_question=f"Test: {team} vs {opponent} Map 1",
            market_type=MarketType.SPORTS,
            asset=sport, direction="WIN",
            team=team, opponent=opponent, sport=sport,
            resolution_dt=close,
            yes_ask_cents=51.0, no_ask_cents=52.0,
            yes_ask_depth=100, no_ask_depth=100,
        )
        poly = NormalizedMarket(
            platform=Platform.POLYMARKET,
            platform_id="poly-test-123",
            platform_url="https://polymarket.com/event/test",
            raw_question=f"{team} vs {opponent}",
            market_type=MarketType.SPORTS,
            asset=sport, direction="WIN",
            team=team, opponent=opponent, sport=sport,
            resolution_dt=close,
            yes_ask_cents=40.0, no_ask_cents=40.0,
            yes_ask_depth=100, no_ask_depth=100,
        )
        return MatchedPair(kalshi=kalshi, poly=poly)

    def test_cs2_verified_match_yields_opportunities(self):
        from scanner.opportunity_finder import OpportunityFinder
        pair = self._make_sports_pair(sport="CS2")
        with _patch_fetch(_SAMPLE_TEAMS):
            opps = OpportunityFinder().find_opportunities([pair])
        assert len(opps) > 0

    def test_cs2_unverified_match_skipped(self):
        from scanner.opportunity_finder import OpportunityFinder
        import scanner.opportunity_finder as of_mod
        pair = self._make_sports_pair(sport="CS2")
        unknown_teams = frozenset(["Some Other Team"])
        # Force validation ON for this test (disabled globally to avoid API costs)
        with patch.object(of_mod, "MATCH_VALIDATION_ENABLED", True), \
             _patch_fetch(unknown_teams):
            opps = OpportunityFinder().find_opportunities([pair])
        assert len(opps) == 0

    def test_cs2_liquipedia_unavailable_still_yields(self):
        """If Liquipedia is down, allow trade (None result)."""
        from scanner.opportunity_finder import OpportunityFinder
        pair = self._make_sports_pair(sport="CS2")
        with _patch_fetch(None):
            opps = OpportunityFinder().find_opportunities([pair])
        assert len(opps) > 0

    def test_lol_verified_match_yields_opportunities(self):
        """LOL pairs are now validated — verified match should produce opportunities."""
        from scanner.opportunity_finder import OpportunityFinder
        lol_teams = frozenset(["Lyon Esports", "Team Liquid"])
        pair = self._make_sports_pair(sport="LOL", team="lyon", opponent="liquid")
        with _patch_fetch(lol_teams):
            opps = OpportunityFinder().find_opportunities([pair])
        assert len(opps) > 0

    def test_lol_unverified_match_skipped(self):
        """LOL pairs not found on Liquipedia should be skipped."""
        from scanner.opportunity_finder import OpportunityFinder
        import scanner.opportunity_finder as of_mod
        pair = self._make_sports_pair(sport="LOL", team="UnknownLOL", opponent="NoTeam")
        # Force validation ON for this test (disabled globally to avoid API costs)
        with patch.object(of_mod, "MATCH_VALIDATION_ENABLED", True), \
             _patch_fetch(frozenset(["T1", "Gen.G"])):
            opps = OpportunityFinder().find_opportunities([pair])
        assert len(opps) == 0

    def test_valorant_verified_match_yields_opportunities(self):
        """VALORANT pairs are now validated."""
        from scanner.opportunity_finder import OpportunityFinder
        val_teams = frozenset(["Novo Esports", "Falke Esports"])
        pair = self._make_sports_pair(sport="VALORANT", team="Novo Esports", opponent="Falke Esports")
        with _patch_fetch(val_teams):
            opps = OpportunityFinder().find_opportunities([pair])
        assert len(opps) > 0

    def test_nba_passes_through_without_validation(self):
        """NBA has no Liquipedia page — pairs pass through silently (no fetch needed)."""
        from scanner.opportunity_finder import OpportunityFinder
        pair = self._make_sports_pair(sport="NBA", team="Lakers", opponent="Celtics")
        with _patch_fetch(_SAMPLE_TEAMS) as mock_fetch:
            opps = OpportunityFinder().find_opportunities([pair])
        # NBA bypasses validation entirely — Liquipedia should never be called
        assert mock_fetch.call_count == 0
        assert len(opps) > 0

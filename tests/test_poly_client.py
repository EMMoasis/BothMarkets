"""Tests for PolyClient normalization, CLOB parsing, and filtering — crypto and sports markets."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from scanner.poly_client import (
    PolyClient,
    _detect_sport_from_text,
    _extract_all_token_ids,
    _extract_yes_no_token_ids,
    _fetch_book,
    _gamma_in_window,
    _is_yes_no_market,
    _normalize_gamma_market,
    _normalize_sports_market,
    _parse_json_field,
)
from scanner.models import MarketType, NormalizedMarket, Platform


# --- Fixtures ---

def _future_iso(hours: int = 24) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_crypto_gamma(
    condition_id="0xABC123",
    question="Will Bitcoin be above $90,000?",
    end_hours=24,
    slug="bitcoin-above-90k-feb21",
    yes_token="YES_TOKEN_ID_123",
    no_token="NO_TOKEN_ID_456",
    yes_ask_cents=57.0,
    yes_bid_cents=55.0,
    no_ask_cents=45.0,
    no_bid_cents=43.0,
    active=True,
    closed=False,
):
    """Create an enriched Gamma market dict for a crypto binary market."""
    gm = {
        "conditionId": condition_id,
        "question": question,
        "endDate": _future_iso(end_hours),
        "slug": slug,
        "active": active,
        "closed": closed,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([yes_token, no_token]),
        "outcomePrices": json.dumps(["0.57", "0.45"]),
        "liquidity": "10000.0",
        "volume": "5000.0",
    }
    # Inject pre-fetched CLOB prices (simulating _enrich_with_clob_prices)
    gm["_clob_prices"] = {
        yes_token: (yes_ask_cents, yes_bid_cents),
        no_token: (no_ask_cents, no_bid_cents),
    }
    return gm


def _make_sports_gamma(
    condition_id="0xSPORTS1",
    question="Counter-Strike: NAVI Junior vs KUUSAMO.gg (BO3) - United21 Group D",
    end_hours=24,
    slug="cs2-navij1-ksm-2026-02-21",
    outcomes=None,
    token_ids=None,
    prices=None,
    active=True,
    closed=False,
):
    """Create an enriched Gamma market dict for a sports moneyline market."""
    if outcomes is None:
        outcomes = ["NAVI Junior", "KUUSAMO.gg"]
    if token_ids is None:
        token_ids = ["TOKEN_NAVI", "TOKEN_KSM"]
    if prices is None:
        # NAVI heavily favored
        prices = {
            "TOKEN_NAVI": (73.5, 72.0),
            "TOKEN_KSM": (26.5, 25.0),
        }

    gm = {
        "conditionId": condition_id,
        "question": question,
        "endDate": _future_iso(end_hours),
        "slug": slug,
        "active": active,
        "closed": closed,
        "sportsMarketType": "moneyline",
        "outcomes": json.dumps(outcomes),
        "clobTokenIds": json.dumps(token_ids),
        "liquidity": "5000.0",
        "volume": "2000.0",
        "_clob_prices": prices,
    }
    return gm


# --- _parse_json_field ---

class TestParseJsonField:
    def test_list_passthrough(self):
        assert _parse_json_field(["Yes", "No"]) == ["Yes", "No"]

    def test_json_string(self):
        assert _parse_json_field('["Yes", "No"]') == ["Yes", "No"]

    def test_invalid_json_returns_none(self):
        assert _parse_json_field("not-json") is None

    def test_none_returns_none(self):
        assert _parse_json_field(None) is None


# --- _is_yes_no_market ---

class TestIsYesNoMarket:
    def test_yes_no(self):
        assert _is_yes_no_market(["Yes", "No"]) is True

    def test_yes_no_any_case(self):
        assert _is_yes_no_market(["YES", "NO"]) is True

    def test_team_names_false(self):
        assert _is_yes_no_market(["NAVI Junior", "KUUSAMO.gg"]) is False

    def test_single_item_false(self):
        assert _is_yes_no_market(["Yes"]) is False

    def test_three_items_false(self):
        assert _is_yes_no_market(["A", "B", "C"]) is False


# --- _detect_sport_from_text ---

class TestDetectSportFromText:
    def test_cs2(self):
        assert _detect_sport_from_text("counter-strike cs2 match") == "CS2"

    def test_nba(self):
        assert _detect_sport_from_text("NBA game tonight") == "NBA"

    def test_nfl(self):
        assert _detect_sport_from_text("NFL playoff game") == "NFL"

    def test_unknown_returns_none(self):
        assert _detect_sport_from_text("Will Bitcoin exceed $90k?") is None


# --- _extract_yes_no_token_ids ---

class TestExtractYesNoTokenIds:
    def test_json_string_format(self):
        gm = {"clobTokenIds": '["YES_ID", "NO_ID"]', "outcomes": '["Yes", "No"]'}
        yes_id, no_id = _extract_yes_no_token_ids(gm)
        assert yes_id == "YES_ID"
        assert no_id == "NO_ID"

    def test_list_format(self):
        gm = {"clobTokenIds": ["YES_ID", "NO_ID"], "outcomes": ["Yes", "No"]}
        yes_id, no_id = _extract_yes_no_token_ids(gm)
        assert yes_id == "YES_ID"
        assert no_id == "NO_ID"

    def test_missing_token_ids_returns_none(self):
        yes_id, no_id = _extract_yes_no_token_ids({})
        assert yes_id is None
        assert no_id is None

    def test_invalid_json_returns_none(self):
        gm = {"clobTokenIds": "not-json"}
        yes_id, no_id = _extract_yes_no_token_ids(gm)
        assert yes_id is None
        assert no_id is None


# --- _extract_all_token_ids ---

class TestExtractAllTokenIds:
    def test_returns_all_tokens(self):
        gm = {"clobTokenIds": '["T1", "T2"]'}
        result = _extract_all_token_ids(gm)
        assert result == ["T1", "T2"]

    def test_sports_three_tokens(self):
        gm = {"clobTokenIds": ["T1", "T2", "T3"]}
        result = _extract_all_token_ids(gm)
        assert result == ["T1", "T2", "T3"]

    def test_empty_returns_empty(self):
        assert _extract_all_token_ids({}) == []


# --- _fetch_book ---

class TestFetchBook:
    def test_returns_ask_and_bid_in_cents(self):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "bids": [
                {"price": "0.50", "size": "100"},
                {"price": "0.55", "size": "200"},  # best bid (last = highest)
            ],
            "asks": [
                {"price": "0.65", "size": "150"},  # first = highest ask
                {"price": "0.57", "size": "300"},  # best ask (last = lowest)
            ],
        }
        mock_http.get.return_value = mock_resp

        ask, bid = _fetch_book(mock_http, "TOKEN_ID")
        assert ask == 57.0    # asks[-1] = 0.57 * 100
        assert bid == 55.0    # bids[-1] = 0.55 * 100

    def test_empty_book_returns_none(self):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"bids": [], "asks": []}
        mock_http.get.return_value = mock_resp

        ask, bid = _fetch_book(mock_http, "TOKEN_ID")
        assert ask is None
        assert bid is None

    def test_exception_returns_none(self):
        mock_http = MagicMock()
        mock_http.get.side_effect = Exception("Connection error")

        ask, bid = _fetch_book(mock_http, "TOKEN_ID")
        assert ask is None
        assert bid is None


# --- _normalize_gamma_market (crypto) ---

class TestNormalizeGammaCrypto:
    def test_btc_market_normalizes_correctly(self):
        gm = _make_crypto_gamma()
        markets = _normalize_gamma_market(gm)
        assert len(markets) == 1
        m = markets[0]
        assert m.platform == Platform.POLYMARKET
        assert m.market_type == MarketType.CRYPTO
        assert m.platform_id == "0xABC123"
        assert m.asset == "BTC"
        assert m.direction == "ABOVE"
        assert m.threshold == 90000.0
        assert m.yes_ask_cents == 57.0
        assert m.no_ask_cents == 45.0
        assert m.yes_token_id == "YES_TOKEN_ID_123"
        assert m.no_token_id == "NO_TOKEN_ID_456"
        assert "polymarket.com/event/bitcoin-above-90k-feb21" in m.platform_url

    def test_missing_condition_id_returns_empty(self):
        gm = _make_crypto_gamma()
        gm["conditionId"] = ""
        assert _normalize_gamma_market(gm) == []

    def test_bad_expiry_returns_empty(self):
        gm = _make_crypto_gamma()
        gm["endDate"] = "not-a-date"
        assert _normalize_gamma_market(gm) == []

    def test_closed_market_returns_empty(self):
        gm = _make_crypto_gamma(closed=True)
        assert _normalize_gamma_market(gm) == []

    def test_inactive_market_returns_empty(self):
        gm = _make_crypto_gamma(active=False)
        assert _normalize_gamma_market(gm) == []

    def test_no_crypto_asset_returns_empty(self):
        gm = _make_crypto_gamma(question="Will it rain in London?")
        # No sports type, no crypto asset → empty
        assert _normalize_gamma_market(gm) == []

    def test_eth_below_market(self):
        gm = _make_crypto_gamma(
            question="Will Ethereum fall below $3,000?",
            condition_id="0xETH1",
        )
        markets = _normalize_gamma_market(gm)
        assert len(markets) == 1
        assert markets[0].asset == "ETH"
        assert markets[0].direction == "BELOW"
        assert markets[0].threshold == 3000.0


# --- _normalize_gamma_market (sports) ---

class TestNormalizeGammaSports:
    def test_moneyline_produces_two_markets(self):
        gm = _make_sports_gamma()
        markets = _normalize_gamma_market(gm)
        # 2 team entries (one per team)
        assert len(markets) == 2

    def test_each_market_has_correct_team(self):
        gm = _make_sports_gamma()
        markets = _normalize_gamma_market(gm)
        teams = {m.team for m in markets}
        # "NAVI Junior" → "navi junior" and "KUUSAMO.gg" → "kuusaoo.gg" (normalized)
        assert any("navi" in t for t in teams)

    def test_sports_market_type(self):
        gm = _make_sports_gamma()
        markets = _normalize_gamma_market(gm)
        assert all(m.market_type == MarketType.SPORTS for m in markets)

    def test_sports_sport_code_cs2(self):
        gm = _make_sports_gamma()
        markets = _normalize_gamma_market(gm)
        assert all(m.sport == "CS2" for m in markets)

    def test_yes_token_is_teams_token(self):
        gm = _make_sports_gamma()
        markets = _normalize_gamma_market(gm)
        token_ids = {m.yes_token_id for m in markets}
        assert "TOKEN_NAVI" in token_ids
        assert "TOKEN_KSM" in token_ids

    def test_no_token_is_opponents_token(self):
        gm = _make_sports_gamma()
        markets = _normalize_gamma_market(gm)
        # Find NAVI's market: its no_token should be KSM's token
        navi_market = next((m for m in markets if "navi" in m.team), None)
        assert navi_market is not None
        assert navi_market.no_token_id == "TOKEN_KSM"

    def test_prices_correctly_assigned(self):
        gm = _make_sports_gamma()
        markets = _normalize_gamma_market(gm)
        navi = next((m for m in markets if "navi" in m.team), None)
        assert navi is not None
        assert navi.yes_ask_cents == 73.5   # NAVI wins token ask
        assert navi.no_ask_cents == 26.5    # KSM wins token ask (opponent)

    def test_event_id_is_condition_id(self):
        gm = _make_sports_gamma()
        markets = _normalize_gamma_market(gm)
        for m in markets:
            assert m.event_id == "0xSPORTS1"

    def test_platform_id_is_synthetic(self):
        gm = _make_sports_gamma()
        markets = _normalize_gamma_market(gm)
        for m in markets:
            # platform_id should be "condition_id_teamname"
            assert m.platform_id.startswith("0xSPORTS1_")

    def test_three_outcome_market_skipped(self):
        # 3-outcome markets are skipped (can't trivially determine opponent)
        gm = _make_sports_gamma(
            outcomes=["Team A", "Team B", "Draw"],
            token_ids=["T1", "T2", "T3"],
            prices={"T1": (40.0, 38.0), "T2": (40.0, 38.0), "T3": (20.0, 18.0)},
        )
        markets = _normalize_gamma_market(gm)
        # 3-outcome → skipped, 0 markets
        assert len(markets) == 0


# --- _gamma_in_window ---

class TestGammaInWindow:
    def test_in_window(self):
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=72)
        gm = {"endDate": (now + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        assert _gamma_in_window(gm, now, cutoff) is True

    def test_past_market(self):
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=72)
        gm = {"endDate": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        assert _gamma_in_window(gm, now, cutoff) is False

    def test_beyond_window(self):
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=72)
        gm = {"endDate": (now + timedelta(hours=80)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        assert _gamma_in_window(gm, now, cutoff) is False

    def test_bad_date_returns_false(self):
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=72)
        gm = {"endDate": "bad-date"}
        assert _gamma_in_window(gm, now, cutoff) is False


# --- PolyClient caching ---

class TestPolyClientCaching:
    def _make_mock_client(self):
        client = PolyClient()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        # Return one crypto gamma market, no CLOB prices (empty book)
        gamma_market = _make_crypto_gamma()
        mock_resp.json.return_value = [gamma_market]
        # Mock CLOB responses (empty books so prices = None)
        clob_resp = MagicMock()
        clob_resp.raise_for_status = MagicMock()
        clob_resp.json.return_value = {"bids": [], "asks": []}
        client._http = MagicMock()
        client._http.get.return_value = mock_resp
        return client

    def test_cache_returns_same_list(self):
        client = self._make_mock_client()
        r1 = client.get_all_markets()
        r2 = client.get_all_markets()
        assert r1 is r2

    def test_force_refresh_bypasses_cache(self):
        client = self._make_mock_client()
        client.get_all_markets()
        initial_call_count = client._http.get.call_count
        client.get_all_markets(force_refresh=True)
        assert client._http.get.call_count > initial_call_count

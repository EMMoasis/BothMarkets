"""Tests for KalshiClient normalization, parsing, and filtering — crypto and sports markets."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from scanner.kalshi_client import (
    KalshiClient,
    _extract_both_teams,
    _get_sport,
    _normalize_one,
    extract_asset,
    extract_direction,
    extract_dollar_amount,
    normalize_team_name,
    parse_iso,
    _to_cents,
)
from scanner.models import MarketType, NormalizedMarket, Platform


# --- Fixtures ---

def _future_iso(hours: int = 24) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_crypto_raw(
    ticker="KXBTC-26FEB21-T90000",
    title="Will BTC be above $90,000 on Feb 21?",
    expiry_hours=24,
    yes_ask=57,
    no_ask=45,
    yes_bid=55,
    no_bid=43,
    series_ticker="KXBTC",
):
    return {
        "ticker": ticker,
        "title": title,
        "expected_expiration_time": _future_iso(expiry_hours),
        "status": "active",
        "series_ticker": series_ticker,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "volume": 10000,
        "liquidity": 5000,
    }


def _make_sports_raw(
    ticker="KXCS2GAME-26FEB22M80VOC-M80",
    title="Will M80 win the M80 vs. Voca CS2 match?",
    expiry_hours=24,
    yes_ask=88,
    no_ask=45,
    yes_bid=85,
    no_bid=42,
    series_ticker="KXCS2GAME",
    yes_sub_title="M80",
    event_ticker="KXCS2GAME-26FEB22M80VOC",
):
    return {
        "ticker": ticker,
        "title": title,
        "expected_expiration_time": _future_iso(expiry_hours),
        "status": "active",
        "series_ticker": series_ticker,
        "yes_sub_title": yes_sub_title,
        "event_ticker": event_ticker,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "volume": 5000,
        "liquidity": 2000,
    }


# --- parse_iso ---

class TestParseIso:
    def test_z_suffix(self):
        dt = parse_iso("2026-02-21T17:00:00Z")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2026

    def test_offset_suffix(self):
        dt = parse_iso("2026-02-21T17:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_microseconds(self):
        dt = parse_iso("2026-02-21T17:00:00.123456Z")
        assert dt is not None

    def test_invalid_returns_none(self):
        assert parse_iso("not-a-date") is None
        assert parse_iso("") is None


# --- extract_asset ---

class TestExtractAsset:
    def test_btc_full(self):
        assert extract_asset("Will Bitcoin be above $90,000?") == "BTC"

    def test_btc_ticker(self):
        assert extract_asset("Will BTC reach $100k?") == "BTC"

    def test_eth(self):
        assert extract_asset("Will Ethereum exceed $3000?") == "ETH"

    def test_xrp(self):
        assert extract_asset("Will XRP be above $2?") == "XRP"

    def test_sol(self):
        assert extract_asset("Will Solana hit $200?") == "SOL"

    def test_unknown_returns_none(self):
        assert extract_asset("Will the Lakers win?") is None

    def test_case_insensitive(self):
        assert extract_asset("Will BITCOIN hit $90K?") == "BTC"


# --- extract_direction ---

class TestExtractDirection:
    def test_above(self):
        assert extract_direction("Will BTC be above $90,000?") == "ABOVE"

    def test_over(self):
        assert extract_direction("Will BTC go over $90k?") == "ABOVE"

    def test_exceed(self):
        assert extract_direction("Will BTC exceed $90k?") == "ABOVE"

    def test_below(self):
        assert extract_direction("Will BTC fall below $80k?") == "BELOW"

    def test_under(self):
        assert extract_direction("Will BTC drop under $80k?") == "BELOW"

    def test_unknown_returns_none(self):
        assert extract_direction("Will the Lakers win the championship?") is None


# --- extract_dollar_amount ---

class TestExtractDollarAmount:
    def test_full_number(self):
        assert extract_dollar_amount("above $90,000") == 90000.0

    def test_k_suffix_lower(self):
        assert extract_dollar_amount("above $90k") == 90000.0

    def test_k_suffix_upper(self):
        assert extract_dollar_amount("above $90K") == 90000.0

    def test_m_suffix(self):
        assert extract_dollar_amount("above $1.5M") == 1_500_000.0

    def test_no_suffix(self):
        assert extract_dollar_amount("above $90000") == 90000.0

    def test_no_amount_returns_none(self):
        assert extract_dollar_amount("Will the Lakers win?") is None

    def test_dollar_2(self):
        assert extract_dollar_amount("Will XRP be above $2?") == 2.0


# --- _to_cents ---

class TestToCents:
    def test_integer(self):
        assert _to_cents(57) == 57.0

    def test_zero(self):
        assert _to_cents(0) == 0.0

    def test_100(self):
        assert _to_cents(100) == 100.0

    def test_none(self):
        assert _to_cents(None) is None

    def test_out_of_range_returns_none(self):
        assert _to_cents(101) is None
        assert _to_cents(-1) is None


# --- normalize_team_name ---

class TestNormalizeTeamName:
    def test_simple(self):
        assert normalize_team_name("M80") == "m80"

    def test_lowercase(self):
        assert normalize_team_name("NAVI Junior") == "navi junior"

    def test_strips_team_prefix(self):
        # "Team Vitality" → "vitality" (strips "team")
        result = normalize_team_name("Team Vitality")
        assert "vitality" in result
        assert "team" not in result

    def test_preserves_single_word(self):
        # Single-word: "Team" alone stays "team" (can't remove if it's the only word)
        result = normalize_team_name("Team")
        assert result == "team"

    def test_fnatic(self):
        assert normalize_team_name("Fnatic") == "fnatic"

    def test_cloud9(self):
        assert normalize_team_name("Cloud9") == "cloud9"

    def test_strips_trailing_number(self):
        assert normalize_team_name("Cloud9 2") == "cloud9"


# --- _extract_both_teams ---

class TestExtractBothTeams:
    def test_simple_vs(self):
        title = "Will M80 win the M80 vs. Voca CS2 match?"
        a, b = _extract_both_teams(title)
        assert a is not None and b is not None
        assert "M80" in a or "m80" in a.lower()
        assert "Voca" in b or "voca" in b.lower()

    def test_multi_word_teams(self):
        title = "Will Fnatic win the Fnatic vs. Team Vitality CS2 match?"
        a, b = _extract_both_teams(title)
        assert a is not None and b is not None
        assert "Fnatic" in a
        assert "Vitality" in b or "Team Vitality" in b

    def test_nba_style(self):
        title = "Will Lakers win the Lakers vs. Celtics NBA match?"
        a, b = _extract_both_teams(title)
        assert a is not None and b is not None

    def test_no_vs_returns_none_none(self):
        title = "Will BTC be above $90,000?"
        a, b = _extract_both_teams(title)
        assert a is None and b is None


# --- _get_sport ---

class TestGetSport:
    def test_cs2game(self):
        assert _get_sport("KXCS2GAME", "") == "CS2"

    def test_cs2game_with_date(self):
        assert _get_sport("KXCS2GAME", "KXCS2GAME-26FEB22M80VOC-M80") == "CS2"

    def test_nba(self):
        assert _get_sport("KXNBAWIN", "") == "NBA"

    def test_nhl(self):
        assert _get_sport("KXNHLWIN", "") == "NHL"

    def test_unknown(self):
        assert _get_sport("KXBTC", "") is None

    def test_ticker_fallback(self):
        # series_ticker empty, detect from ticker
        assert _get_sport("", "KXCS2GAME-26FEB22M80VOC-M80") == "CS2"


# --- _normalize_one (crypto) ---

class TestNormalizeOneCrypto:
    def test_btc_market_normalizes_correctly(self):
        raw = _make_crypto_raw()
        m = _normalize_one(raw)
        assert m is not None
        assert m.platform == Platform.KALSHI
        assert m.market_type == MarketType.CRYPTO
        assert m.platform_id == "KXBTC-26FEB21-T90000"
        assert m.asset == "BTC"
        assert m.direction == "ABOVE"
        assert m.threshold == 90000.0
        assert m.yes_ask_cents == 57.0
        assert m.no_ask_cents == 45.0
        assert "kalshi.com/markets/KXBTC-26FEB21-T90000" in m.platform_url

    def test_missing_ticker_returns_none(self):
        raw = _make_crypto_raw()
        raw["ticker"] = ""
        assert _normalize_one(raw) is None

    def test_bad_expiry_returns_none(self):
        raw = _make_crypto_raw()
        raw["expected_expiration_time"] = "not-a-date"
        assert _normalize_one(raw) is None

    def test_no_asset_crypto_returns_none(self):
        # No crypto keywords → not a crypto market; no sports series → None
        raw = _make_crypto_raw(
            title="Will it rain in London?",
            series_ticker="KXUNKNOWN",
        )
        assert _normalize_one(raw) is None

    def test_eth_below_market(self):
        raw = _make_crypto_raw(
            ticker="KXETH-26FEB21-T3000",
            title="Will ETH fall below $3,000 on Feb 21?",
        )
        m = _normalize_one(raw)
        assert m is not None
        assert m.asset == "ETH"
        assert m.direction == "BELOW"
        assert m.threshold == 3000.0


# --- _normalize_one (sports) ---

class TestNormalizeOneSports:
    def test_cs2_market_normalizes(self):
        raw = _make_sports_raw()
        m = _normalize_one(raw)
        assert m is not None
        assert m.platform == Platform.KALSHI
        assert m.market_type == MarketType.SPORTS
        assert m.sport == "CS2"
        assert m.team == "m80"        # normalized
        assert m.opponent == "voca"   # normalized from title
        assert m.direction == "WIN"
        assert m.yes_ask_cents == 88.0
        assert m.no_ask_cents == 45.0
        assert "kalshi.com/markets/" in m.platform_url

    def test_sports_market_has_correct_event_id(self):
        raw = _make_sports_raw()
        m = _normalize_one(raw)
        assert m is not None
        assert m.event_id == "KXCS2GAME-26FEB22M80VOC"

    def test_missing_yes_sub_title_uses_title_fallback(self):
        raw = _make_sports_raw()
        raw["yes_sub_title"] = ""
        m = _normalize_one(raw)
        # Should still parse from title "Will M80 win the ..."
        assert m is not None
        assert "m80" in m.team

    def test_sports_market_no_threshold(self):
        raw = _make_sports_raw()
        m = _normalize_one(raw)
        assert m is not None
        assert m.threshold == 0.0


# --- KalshiClient._filter_by_window ---

class TestFilterByWindow:
    def test_keeps_markets_in_72h(self):
        client = KalshiClient()
        m_in = _normalize_one(_make_crypto_raw(expiry_hours=48))
        m_out = _normalize_one(_make_crypto_raw(expiry_hours=80, ticker="K2"))
        markets = [x for x in [m_in, m_out] if x is not None]
        filtered = client._filter_by_window(markets)
        assert len(filtered) == 1
        assert filtered[0].platform_id == "KXBTC-26FEB21-T90000"

    def test_excludes_past_markets(self):
        client = KalshiClient()
        raw = _make_crypto_raw()
        raw["expected_expiration_time"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        m = _normalize_one(raw)
        assert m is not None
        assert client._filter_by_window([m]) == []

    def test_sports_market_in_72h(self):
        client = KalshiClient()
        m = _normalize_one(_make_sports_raw(expiry_hours=10))
        assert m is not None
        assert len(client._filter_by_window([m])) == 1


# --- KalshiClient._fetch_all_pages (mocked) ---

class TestFetchAllPages:
    def test_single_page_no_cursor(self):
        client = KalshiClient()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"markets": [_make_crypto_raw()], "cursor": None}
        client._http = MagicMock()
        client._http.get.return_value = mock_resp

        result = client._fetch_all_pages()
        assert len(result) == 1

    def test_two_pages_with_cursor(self):
        client = KalshiClient()
        page1 = MagicMock()
        page1.raise_for_status = MagicMock()
        page1.json.return_value = {
            "markets": [_make_crypto_raw(ticker=f"K{i}") for i in range(1000)],
            "cursor": "cursor_abc",
        }
        page2 = MagicMock()
        page2.raise_for_status = MagicMock()
        page2.json.return_value = {"markets": [_make_crypto_raw(ticker="K_last")], "cursor": None}
        client._http = MagicMock()
        client._http.get.side_effect = [page1, page2]

        result = client._fetch_all_pages()
        assert len(result) == 1001

    def test_empty_response(self):
        client = KalshiClient()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"markets": [], "cursor": None}
        client._http = MagicMock()
        client._http.get.return_value = mock_resp

        result = client._fetch_all_pages()
        assert result == []


# --- KalshiClient caching ---

class TestCaching:
    def test_cache_returns_same_list(self):
        client = KalshiClient()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"markets": [_make_crypto_raw()], "cursor": None}
        client._http = MagicMock()
        client._http.get.return_value = mock_resp

        r1 = client.get_all_markets()
        r2 = client.get_all_markets()
        assert client._http.get.call_count == 1
        assert r1 is r2

    def test_force_refresh_bypasses_cache(self):
        client = KalshiClient()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"markets": [_make_crypto_raw()], "cursor": None}
        client._http = MagicMock()
        client._http.get.return_value = mock_resp

        client.get_all_markets()
        client.get_all_markets(force_refresh=True)
        assert client._http.get.call_count == 2

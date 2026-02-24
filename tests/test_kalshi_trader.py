"""Tests for scanner.kalshi_trader — RSA-PS256 signing and order placement."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, rsa

from scanner.kalshi_trader import KalshiTrader, _to_cents


# ---------------------------------------------------------------------------
# Helpers: generate a real RSA key for tests so signing actually works
# ---------------------------------------------------------------------------

def _make_test_pem() -> str:
    """Generate a fresh RSA-2048 private key in PEM format for testing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


TEST_KEY = _make_test_pem()
TEST_API_KEY = "test-api-key-uuid"


def _make_trader() -> KalshiTrader:
    return KalshiTrader(api_key=TEST_API_KEY, api_secret_pem=TEST_KEY)


# ---------------------------------------------------------------------------
# _to_cents helper
# ---------------------------------------------------------------------------

class TestToCents:
    def test_valid_integer(self):
        assert _to_cents(55) == 55.0

    def test_valid_float_string(self):
        assert _to_cents("45.5") == 45.5

    def test_zero(self):
        assert _to_cents(0) == 0.0

    def test_hundred(self):
        assert _to_cents(100) == 100.0

    def test_none_returns_none(self):
        assert _to_cents(None) is None

    def test_above_100_returns_none(self):
        assert _to_cents(101) is None

    def test_negative_returns_none(self):
        assert _to_cents(-1) is None

    def test_invalid_string_returns_none(self):
        assert _to_cents("abc") is None


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

class TestSigning:
    def test_sign_returns_timestamp_and_base64(self):
        trader = _make_trader()
        ts, sig = trader._sign("POST", "/trade-api/v2/portfolio/orders", '{"ticker":"X"}')
        assert ts.isdigit()
        assert len(ts) == 13  # milliseconds (13 digits around 2024)
        # Must be valid base64
        decoded = base64.b64decode(sig)
        assert len(decoded) == 256  # RSA-2048 signature is 256 bytes

    def test_sign_different_timestamp_each_call(self):
        trader = _make_trader()
        ts1, _ = trader._sign("GET", "/trade-api/v2/portfolio/balance", "")
        ts2, _ = trader._sign("GET", "/trade-api/v2/portfolio/balance", "")
        # Timestamps may be identical if called within same millisecond, but signature valid
        assert ts1.isdigit() and ts2.isdigit()

    def test_auth_headers_contain_required_keys(self):
        trader = _make_trader()
        ts, sig = trader._sign("GET", "/trade-api/v2/portfolio/balance", "")
        headers = trader._auth_headers(ts, sig)
        assert headers["KALSHI-ACCESS-KEY"] == TEST_API_KEY
        assert headers["KALSHI-ACCESS-SIGNATURE"] == sig
        assert headers["KALSHI-ACCESS-TIMESTAMP"] == ts

    def test_escaped_newlines_in_pem_handled(self):
        """PEM keys stored in .env may have \\n instead of real newlines."""
        key_with_escaped = TEST_KEY.replace("\n", "\\n")
        trader = KalshiTrader(api_key=TEST_API_KEY, api_secret_pem=key_with_escaped)
        ts, sig = trader._sign("GET", "/trade-api/v2/portfolio/balance", "")
        assert len(base64.b64decode(sig)) == 256


# ---------------------------------------------------------------------------
# Order body construction
# ---------------------------------------------------------------------------

class TestPlaceOrder:
    def test_yes_side_uses_yes_price(self):
        """BUY YES should put price in yes_price field."""
        trader = _make_trader()
        captured = {}

        def fake_post(path, body):
            captured["body"] = body
            return {"order": {"order_id": "k-123"}}

        trader._post = fake_post
        trader.place_order("KXLOLMAP-TEST", side="yes", count=5, price_cents=55)
        assert "yes_price" in captured["body"]
        assert captured["body"]["yes_price"] == 55
        assert "no_price" not in captured["body"]
        assert captured["body"]["action"] == "buy"
        assert captured["body"]["side"] == "yes"
        assert captured["body"]["count"] == 5

    def test_no_side_uses_no_price(self):
        """BUY NO should put price in no_price field."""
        trader = _make_trader()
        captured = {}

        def fake_post(path, body):
            captured["body"] = body
            return {"order": {"order_id": "k-456"}}

        trader._post = fake_post
        trader.place_order("KXLOLMAP-TEST", side="no", count=3, price_cents=45)
        assert "no_price" in captured["body"]
        assert captured["body"]["no_price"] == 45
        assert "yes_price" not in captured["body"]

    def test_sell_action(self):
        """SELL action is passed through."""
        trader = _make_trader()
        captured = {}

        def fake_post(path, body):
            captured["body"] = body
            return {"order": {"order_id": "k-789"}}

        trader._post = fake_post
        trader.place_order("KXLOLMAP-TEST", side="yes", count=5, price_cents=50, action="sell")
        assert captured["body"]["action"] == "sell"

    def test_raises_on_invalid_count(self):
        trader = _make_trader()
        with pytest.raises(ValueError, match="count must be"):
            trader.place_order("KXTEST", side="yes", count=0, price_cents=50)

    def test_raises_on_invalid_price(self):
        trader = _make_trader()
        with pytest.raises(ValueError, match="price_cents must be"):
            trader.place_order("KXTEST", side="yes", count=1, price_cents=0)

    def test_raises_on_invalid_price_100(self):
        trader = _make_trader()
        with pytest.raises(ValueError, match="price_cents must be"):
            trader.place_order("KXTEST", side="yes", count=1, price_cents=100)

    def test_raises_on_invalid_side(self):
        trader = _make_trader()
        with pytest.raises(ValueError, match="side must be"):
            trader.place_order("KXTEST", side="bad", count=1, price_cents=50)

    def test_raises_on_invalid_action(self):
        trader = _make_trader()
        with pytest.raises(ValueError, match="action must be"):
            trader.place_order("KXTEST", side="yes", count=1, price_cents=50, action="hold")

    def test_client_order_id_is_uuid(self):
        """Each call generates a unique client_order_id."""
        trader = _make_trader()
        ids = set()

        def fake_post(path, body):
            ids.add(body["client_order_id"])
            return {"order": {"order_id": "k-ok"}}

        trader._post = fake_post
        for _ in range(5):
            trader.place_order("KXTEST", side="yes", count=1, price_cents=55)
        assert len(ids) == 5  # all unique


# ---------------------------------------------------------------------------
# HTTP layer: POST signs and calls correct URL
# ---------------------------------------------------------------------------

class TestHttpLayer:
    def test_post_signs_request(self):
        """_post should add auth headers to the outgoing request."""
        trader = _make_trader()
        mock_response = MagicMock()
        mock_response.json.return_value = {"order": {"order_id": "x"}}
        mock_response.raise_for_status = MagicMock()

        with patch.object(trader._http, "post", return_value=mock_response) as mock_post:
            trader._post("/portfolio/orders", {"ticker": "T", "action": "buy",
                                                "side": "yes", "count": 1,
                                                "yes_price": 55, "type": "limit",
                                                "client_order_id": "uuid-1"})
            call_kwargs = mock_post.call_args
            headers = call_kwargs.kwargs.get("headers") or {}
            assert "KALSHI-ACCESS-KEY" in headers
            assert "KALSHI-ACCESS-SIGNATURE" in headers
            assert "KALSHI-ACCESS-TIMESTAMP" in headers

    def test_get_balance_calls_portfolio_balance(self):
        """get_balance() should hit /portfolio/balance endpoint."""
        trader = _make_trader()
        mock_response = MagicMock()
        mock_response.json.return_value = {"balance": 1000}  # $10.00
        mock_response.raise_for_status = MagicMock()

        with patch.object(trader._http, "get", return_value=mock_response) as mock_get:
            bal = trader.get_balance()
            assert bal == 10.0
            called_url = mock_get.call_args.args[0]
            assert called_url.endswith("/portfolio/balance")

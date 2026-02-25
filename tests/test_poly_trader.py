"""Tests for scanner.poly_trader — Polymarket order placement via ClobClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scanner.poly_trader import PolyTrader


# ---------------------------------------------------------------------------
# Helpers: build a PolyTrader with a fully mocked ClobClient
# ---------------------------------------------------------------------------

def _make_trader() -> tuple[PolyTrader, MagicMock]:
    """Return (PolyTrader, mock_client) where ClobClient is fully mocked."""
    with patch("scanner.poly_trader.ClobClient") as MockClient:
        mock_client = MagicMock()
        MockClient.return_value = mock_client
        trader = PolyTrader(
            private_key="0xdeadbeef",
            api_key="test-api-key",
            api_secret="test-secret",
            api_passphrase="test-pass",
            funder="0xfunder",
        )
        trader._client = mock_client
        return trader, mock_client


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInit:
    def test_initializes_with_all_creds(self):
        """PolyTrader initializes without error when all creds provided."""
        with patch("scanner.poly_trader.ClobClient") as MockClient:
            MockClient.return_value = MagicMock()
            trader = PolyTrader(
                private_key="0xkey",
                api_key="k",
                api_secret="s",
                api_passphrase="p",
                funder="0xfund",   # accepted for backward compat, not forwarded
            )
            assert trader is not None
            # sig_type=0: funder is NOT passed to ClobClient (EOA mode)
            call_kwargs = MockClient.call_args.kwargs
            assert call_kwargs.get("signature_type") == 0
            assert "funder" not in call_kwargs

    def test_initializes_without_funder(self):
        """funder is optional — should not be passed to ClobClient when absent."""
        with patch("scanner.poly_trader.ClobClient") as MockClient:
            MockClient.return_value = MagicMock()
            PolyTrader(
                private_key="0xkey",
                api_key="k",
                api_secret="s",
                api_passphrase="p",
            )
            call_kwargs = MockClient.call_args.kwargs
            assert "funder" not in call_kwargs

    def test_auto_derives_keys_when_none_supplied(self):
        """When api_key/secret/passphrase are blank, auto-derive from private key."""
        with patch("scanner.poly_trader.ClobClient") as MockClient:
            mock_l1 = MagicMock()
            mock_creds = MagicMock()
            mock_creds.api_key = "auto-key"
            mock_creds.api_secret = "auto-secret"
            mock_creds.api_passphrase = "auto-pass"
            mock_l1.derive_api_key.return_value = mock_creds
            MockClient.return_value = mock_l1

            trader = PolyTrader(private_key="0xkey")   # no api_key supplied
            assert trader is not None
            # derive_api_key should have been called
            mock_l1.derive_api_key.assert_called_once()


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------

class TestPlaceOrder:
    def test_buy_calls_create_and_post(self):
        """place_order should call create_order then post_order."""
        trader, mock_client = _make_trader()
        mock_client.create_order.return_value = MagicMock(name="signed_order")
        mock_client.post_order.return_value = {"orderID": "poly-abc"}

        result = trader.place_order(
            token_id="token-123",
            price=0.55,
            size=10.0,
            side="BUY",
        )

        assert mock_client.create_order.called
        assert mock_client.post_order.called
        assert result["orderID"] == "poly-abc"

    def test_buy_passes_correct_args(self):
        """OrderArgs should be created with correct token_id, price, size."""
        trader, mock_client = _make_trader()
        mock_client.create_order.return_value = MagicMock()
        mock_client.post_order.return_value = {"orderID": "oid"}

        with patch("scanner.poly_trader.OrderArgs") as MockOrderArgs:
            MockOrderArgs.return_value = MagicMock()
            trader.place_order(token_id="tok-xyz", price=0.42, size=5.0)
            call_args = MockOrderArgs.call_args
            assert call_args.kwargs["token_id"] == "tok-xyz"
            assert call_args.kwargs["price"] == 0.42
            assert call_args.kwargs["size"] == 5.0

    def test_uses_fok_order_type(self):
        """post_order must always be called with FOK order type."""
        trader, mock_client = _make_trader()
        mock_client.create_order.return_value = MagicMock()
        mock_client.post_order.return_value = {"orderID": "oid"}

        from py_clob_client.clob_types import OrderType

        trader.place_order(token_id="t", price=0.5, size=3.0)
        call_kwargs = mock_client.post_order.call_args.kwargs
        assert call_kwargs.get("orderType") == OrderType.FOK

    def test_sell_order(self):
        """SELL side should pass SELL constant to OrderArgs."""
        trader, mock_client = _make_trader()
        mock_client.create_order.return_value = MagicMock()
        mock_client.post_order.return_value = {"orderID": "oid"}

        from py_clob_client.order_builder.constants import SELL

        with patch("scanner.poly_trader.OrderArgs") as MockOrderArgs:
            MockOrderArgs.return_value = MagicMock()
            trader.place_order(token_id="t", price=0.6, size=2.0, side="SELL")
            side_arg = MockOrderArgs.call_args.kwargs["side"]
            assert side_arg == SELL


# ---------------------------------------------------------------------------
# get_usdc_balance
# ---------------------------------------------------------------------------

class TestGetBalance:
    def test_converts_raw_to_dollars(self):
        """Raw balance 5_000_000 (USDC 6 decimals) → $5.00."""
        trader, mock_client = _make_trader()
        mock_client.get_balance_allowance.return_value = {"balance": 5_000_000}
        assert trader.get_usdc_balance() == 5.0

    def test_zero_balance(self):
        trader, mock_client = _make_trader()
        mock_client.get_balance_allowance.return_value = {"balance": 0}
        assert trader.get_usdc_balance() == 0.0

    def test_missing_balance_field(self):
        trader, mock_client = _make_trader()
        mock_client.get_balance_allowance.return_value = {}
        assert trader.get_usdc_balance() == 0.0


# ---------------------------------------------------------------------------
# get_actual_fill
# ---------------------------------------------------------------------------

class TestGetActualFill:
    def test_returns_size_matched_when_present(self):
        trader, mock_client = _make_trader()
        mock_client.get_order.return_value = {"size_matched": "7.5"}
        assert trader.get_actual_fill("oid-1", 10.0) == 7.5

    def test_returns_estimated_when_missing(self):
        trader, mock_client = _make_trader()
        mock_client.get_order.return_value = {}
        assert trader.get_actual_fill("oid-1", 10.0) == 10.0

    def test_returns_estimated_on_exception(self):
        trader, mock_client = _make_trader()
        mock_client.get_order.side_effect = Exception("network error")
        assert trader.get_actual_fill("oid-1", 8.0) == 8.0

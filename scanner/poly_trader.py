"""Polymarket order placement via py-clob-client.

Uses the same ClobClient pattern as the polytrader project.

Credentials (environment variables):
  POLY_PRIVATE_KEY     — Ethereum private key (0x...)
  POLY_API_KEY         — CLOB API key
  POLY_API_SECRET      — CLOB API secret
  POLY_API_PASSPHRASE  — CLOB API passphrase
  POLY_FUNDER          — Funder wallet address (for USDC collateral)

Order flow:
  1. client.create_order(OrderArgs(token_id, price_0_to_1, size, BUY))
  2. client.post_order(signed_order, orderType=OrderType.FOK)
  3. Query fill: client.get_order(order_id)

Prices are always in 0-1 float range (0.55 = 55c).
Sizes are share counts (float, Polymarket minimum ~$1/leg).
"""

from __future__ import annotations

import logging
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

log = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
POLY_CHAIN_ID = 137  # Polygon mainnet


class PolyTrader:
    """
    Places and manages orders on Polymarket using py-clob-client.

    All prices are 0.0–1.0 floats (e.g. 0.55 = 55c).
    Sizes are share counts (float).

    Uses FOK (Fill or Kill) order type: either fills immediately or is cancelled.
    """

    def __init__(
        self,
        private_key: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        funder: str | None = None,
    ) -> None:
        creds = ApiCreds(
            api_key=api_key.strip(),
            api_secret=api_secret.strip(),
            api_passphrase=api_passphrase.strip(),
        )
        kwargs: dict[str, Any] = {
            "host": CLOB_HOST,
            "chain_id": POLY_CHAIN_ID,
            "key": private_key.strip(),
            "creds": creds,
            "signature_type": 2,
        }
        if funder:
            kwargs["funder"] = funder.strip()
        self._client = ClobClient(**kwargs)
        log.info("PolyTrader initialized, funder=%s", funder or "EOA")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_usdc_balance(self) -> float:
        """Return available USDC balance in dollars.

        Raw balance from API is in USDC base units (6 decimals).
        100 USDC = 100_000_000 raw → divide by 1_000_000.
        """
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = self._client.get_balance_allowance(params)
        raw = float(result.get("balance", 0))
        return raw / 1_000_000

    def place_order(
        self,
        token_id: str,
        price: float,       # 0.0–1.0  (e.g. 0.55 = 55 cents)
        size: float,        # shares to buy/sell
        side: str = "BUY",  # "BUY" or "SELL"
    ) -> dict[str, Any]:
        """
        Place a FOK limit order on Polymarket.

        price: float in 0.0–1.0 range
        size:  number of shares
        Returns the order response dict (contains 'orderID' on success).
        Raises on API errors.
        """
        clob_side = BUY if side.upper() == "BUY" else SELL
        signed = self._client.create_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=clob_side,
            )
        )
        result = self._client.post_order(signed, orderType=OrderType.FOK)
        order_id = result.get("orderID") or result.get("id") or "N/A"
        log.info(
            "Poly order: %s token=%s... size=%.2f @ %.4f → id=%s",
            side.upper(), token_id[:16], size, price, order_id,
        )
        return result

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch current fill info for a Polymarket order."""
        return self._client.get_order(order_id)

    def get_actual_fill(self, order_id: str, estimated_size: float) -> float:
        """Return actual matched share count for a FOK order.

        Polymarket fills as many shares as possible within the (size × price) budget,
        so size_matched often differs from the requested size.
        Falls back to estimated_size on any error.
        """
        try:
            data = self._client.get_order(order_id)
            matched = data.get("size_matched")
            if matched is not None:
                return float(matched)
        except Exception:
            log.warning("Could not query fill size for order %s", order_id)
        return estimated_size

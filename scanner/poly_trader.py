"""Polymarket order placement via py-clob-client.

Uses the same ClobClient pattern as the polytrader project.

Credentials (environment variables):
  POLY_PRIVATE_KEY     — Ethereum private key (hex, no 0x prefix required)
  POLY_API_KEY         — CLOB API key  (auto-created on first init if blank)
  POLY_API_SECRET      — CLOB API secret
  POLY_API_PASSPHRASE  — CLOB API passphrase
  POLY_FUNDER          — Proxy wallet address that holds USDC (Bot address)

Signature modes:
  sig_type=2 (proxy): when POLY_FUNDER is set and differs from the key's address.
                       maker=funder (proxy wallet), signer=private_key address.
                       Balance comes from the proxy wallet's off-chain CLOB ledger.
  sig_type=0 (EOA):   fallback when no funder supplied.
                       Requires on-chain USDC in the wallet.

Order flow:
  1. client.create_order(OrderArgs(token_id, price_0_to_1, size, BUY/SELL))
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
_KEY_NONCE = 0       # Deterministic nonce for derive/create


def _build_creds(private_key: str, sig_type: int = 0) -> ApiCreds:
    """
    Derive (or create) API credentials for the given private key.

    Tries derive_api_key first (idempotent).  Falls back to create_api_key
    if the key doesn't exist yet.
    """
    l1 = ClobClient(
        host=CLOB_HOST,
        chain_id=POLY_CHAIN_ID,
        key=private_key.strip(),
        signature_type=sig_type,
    )
    try:
        c = l1.derive_api_key(nonce=_KEY_NONCE)
        log.info("PolyTrader: derived existing API key %s (nonce=%d)", c.api_key, _KEY_NONCE)
        return ApiCreds(api_key=c.api_key, api_secret=c.api_secret, api_passphrase=c.api_passphrase)
    except Exception:
        pass
    c = l1.create_api_key(nonce=_KEY_NONCE)
    log.info("PolyTrader: created new API key %s (nonce=%d)", c.api_key, _KEY_NONCE)
    return ApiCreds(api_key=c.api_key, api_secret=c.api_secret, api_passphrase=c.api_passphrase)


class PolyTrader:
    """
    Places and manages orders on Polymarket using py-clob-client.

    All prices are 0.0–1.0 floats (e.g. 0.55 = 55c).
    Sizes are share counts (float).

    Uses FOK (Fill or Kill) order type: either fills immediately or is cancelled.

    Signature mode is selected automatically:
      - funder provided → sig_type=2 (proxy): maker=funder, signer=private_key address.
        Balance comes from the funder proxy wallet (Bot address on Polymarket web UI).
        No on-chain USDC needed in the signing key's wallet.
      - no funder       → sig_type=0 (EOA):   wallet must hold USDC on Polygon.
    """

    def __init__(
        self,
        private_key: str,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        funder: str | None = None,
    ) -> None:
        pk = private_key.strip()

        # Select signature type based on whether a distinct proxy funder is supplied
        funder_addr = funder.strip() if funder else None
        sig_type = 2 if funder_addr else 0

        # API credentials: use supplied or auto-derive
        if api_key and api_secret and api_passphrase:
            creds = ApiCreds(
                api_key=api_key.strip(),
                api_secret=api_secret.strip(),
                api_passphrase=api_passphrase.strip(),
            )
        else:
            log.info("PolyTrader: no API key supplied — auto-deriving from private key")
            creds = _build_creds(pk, sig_type=sig_type)

        # Build client
        client_kwargs: dict[str, Any] = dict(
            host=CLOB_HOST,
            chain_id=POLY_CHAIN_ID,
            key=pk,
            creds=creds,
            signature_type=sig_type,
        )
        if funder_addr:
            client_kwargs["funder"] = funder_addr

        self._client = ClobClient(**client_kwargs)
        self._sig_type = sig_type
        log.info(
            "PolyTrader initialized (sig_type=%d/%s, funder=%s)",
            sig_type,
            "proxy" if sig_type == 2 else "EOA",
            funder_addr or "self",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_usdc_balance(self) -> float:
        """Return available USDC balance in dollars.

        For sig_type=2 (proxy) this is the funder wallet's off-chain CLOB balance.
        For sig_type=0 (EOA) this is the on-chain USDC balance.
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

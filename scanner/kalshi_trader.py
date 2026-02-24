"""Kalshi order placement via RSA-PS256 authenticated REST API.

Places limit BUY/SELL orders using the Kalshi v2 trading API.

Auth headers (per request):
  KALSHI-ACCESS-KEY         — API key ID (UUID)
  KALSHI-ACCESS-SIGNATURE   — base64(RSA-PS256(timestamp + METHOD + path + body))
  KALSHI-ACCESS-TIMESTAMP   — milliseconds since epoch (string)

Order format:
  POST /trade-api/v2/portfolio/orders
  {
    "ticker":           "KXLOLMAP-...",
    "client_order_id":  "uuid",
    "type":             "limit",
    "action":           "buy" | "sell",
    "side":             "yes" | "no",
    "count":            <int>,
    "yes_price":        <int cents>   # when side == "yes"
    "no_price":         <int cents>   # when side == "no"
  }
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

from scanner.config import HTTP_TIMEOUT, KALSHI_BASE_URL

log = logging.getLogger(__name__)

# The path prefix used for signing (everything after the domain)
_API_PATH_PREFIX = "/trade-api/v2"


class KalshiTrader:
    """
    Places and manages orders on Kalshi using RSA-PS256 authentication.

    Credentials:
      api_key:        KALSHI_API_KEY env var  — key ID (UUID)
      api_secret_pem: KALSHI_API_SECRET env var — RSA private key (PEM)

    All prices are in integer CENTS (1–99).
    """

    def __init__(self, api_key: str, api_secret_pem: str) -> None:
        self._api_key = api_key.strip()
        # Handle escaped newlines from .env files
        pem = api_secret_pem.strip().replace("\\n", "\n")
        self._private_key = serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=None,
        )
        self._http = httpx.Client(
            timeout=HTTP_TIMEOUT,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """Return Kalshi account balance in dollars (balance field is in cents)."""
        data = self._get("/portfolio/balance")
        return float(data.get("balance", 0)) / 100.0

    def place_order(
        self,
        ticker: str,
        side: str,           # "yes" or "no"
        count: int,          # number of contracts (integer ≥ 1)
        price_cents: int,    # integer 1–99
        action: str = "buy", # "buy" or "sell"
    ) -> dict[str, Any]:
        """
        Place a limit order on Kalshi.

        Returns the API response dict (contains 'order' key with order details).
        Raises httpx.HTTPStatusError on API errors (4xx / 5xx).
        """
        if count < 1:
            raise ValueError(f"count must be ≥ 1, got {count}")
        if not (1 <= price_cents <= 99):
            raise ValueError(f"price_cents must be 1–99, got {price_cents}")
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")

        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "type": "limit",
            "action": action,
            "side": side,
            "count": count,
        }
        # Kalshi uses yes_price / no_price rather than a generic price field
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents

        resp = self._post("/portfolio/orders", body)

        order_id = (resp.get("order") or {}).get("order_id", "N/A")
        log.info(
            "Kalshi order: %s %s %s ×%d @ %dc → id=%s",
            action.upper(), side.upper(), ticker, count, price_cents, order_id,
        )
        return resp

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an open Kalshi order by order ID."""
        return self._delete(f"/portfolio/orders/{order_id}")

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch current status/fill info for a Kalshi order."""
        return self._get(f"/portfolio/orders/{order_id}")

    def get_market_price(self, ticker: str) -> dict[str, float | None]:
        """Fetch current yes/no bid and ask prices for a ticker (in cents)."""
        data = self._get(f"/markets/{ticker}")
        mkt = data.get("market", {})
        return {
            "yes_ask": _to_cents(mkt.get("yes_ask")),
            "no_ask":  _to_cents(mkt.get("no_ask")),
            "yes_bid": _to_cents(mkt.get("yes_bid")),
            "no_bid":  _to_cents(mkt.get("no_bid")),
        }

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> dict[str, Any]:
        full_path = f"{_API_PATH_PREFIX}{path}"
        ts, sig = self._sign("GET", full_path, "")
        resp = self._http.get(
            f"{KALSHI_BASE_URL}{path}",
            headers=self._auth_headers(ts, sig),
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        full_path = f"{_API_PATH_PREFIX}{path}"
        body_json = json.dumps(body, separators=(",", ":"))
        ts, sig = self._sign("POST", full_path, body_json)
        resp = self._http.post(
            f"{KALSHI_BASE_URL}{path}",
            content=body_json,
            headers=self._auth_headers(ts, sig),
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict[str, Any]:
        full_path = f"{_API_PATH_PREFIX}{path}"
        ts, sig = self._sign("DELETE", full_path, "")
        resp = self._http.delete(
            f"{KALSHI_BASE_URL}{path}",
            headers=self._auth_headers(ts, sig),
        )
        resp.raise_for_status()
        return resp.json()

    def _sign(self, method: str, path: str, body: str) -> tuple[str, str]:
        """Generate RSA-PS256 signature for a Kalshi API request.

        Message format: timestamp_ms + METHOD_UPPERCASE + path + body
        Returns (timestamp_ms_string, base64url_signature).
        """
        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + path + body).encode("utf-8")
        sig_bytes = self._private_key.sign(
            message,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=asym_padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return ts, base64.b64encode(sig_bytes).decode("utf-8")

    def _auth_headers(self, timestamp: str, signature: str) -> dict[str, str]:
        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _to_cents(val: Any) -> float | None:
    """Convert Kalshi price field (integer cents 0-100) to float. None if invalid."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if 0.0 <= f <= 100.0 else None
    except (TypeError, ValueError):
        return None

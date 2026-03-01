"""
Close the naked short Kalshi position on KXLOLMAP-26MAR01FOXGEN-1-FOX.

Current position: -520 FOX YES (short — scanner sold contracts it never owned).
To close: BUY 520 FOX YES at current ask.

Run:  py -3 close_foxgen_short.py
"""
import os, sys
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

from scanner.kalshi_trader import KalshiTrader

TICKER = "KXLOLMAP-26MAR01FOXGEN-1-FOX"
SHORT_UNITS = 520

kt = KalshiTrader(
    api_key=os.environ["KALSHI_API_KEY"],
    api_secret_pem=os.environ["KALSHI_API_SECRET"],
)

# 1. Check current position
print("=== FOXGEN Short Close ===")
bal = kt.get_balance()
print(f"Kalshi balance: ${bal:.2f}")

data = kt._get("/portfolio/positions")
foxgen_pos = next(
    (p for p in data.get("market_positions", []) if p.get("ticker") == TICKER),
    None,
)
if foxgen_pos is None:
    print("No FOXGEN position found — nothing to close.")
    sys.exit(0)

position = foxgen_pos.get("position", 0)
print(f"Current FOXGEN position: {position}")
if position >= 0:
    print("Position is not short — nothing to close.")
    sys.exit(0)

units_to_buy = abs(position)
print(f"Need to BUY {units_to_buy} FOX YES to close short.")

# 2. Get current ask
prices = kt.get_market_price(TICKER)
yes_ask = prices.get("yes_ask")
print(f"Current FOX YES ask: {yes_ask}c")

if yes_ask is None:
    print("ERROR: Cannot read YES ask price — aborting.")
    sys.exit(1)

cost = units_to_buy * yes_ask / 100.0
print(f"Estimated cost to close: ${cost:.2f} ({units_to_buy} × {yes_ask}c)")
print()

confirm = input(f"Type YES to place BUY {units_to_buy} FOX YES @ {int(yes_ask)}c: ").strip()
if confirm.upper() != "YES":
    print("Aborted.")
    sys.exit(0)

# 3. Place BUY order
resp = kt.place_order(
    ticker=TICKER,
    side="yes",
    count=units_to_buy,
    price_cents=int(yes_ask),
    action="buy",
)
order_id = (resp.get("order") or {}).get("order_id", "?")
print(f"Order placed: {order_id}")

import time
time.sleep(1)
order_info = kt.get_order(order_id)
order = order_info.get("order", {})
status = order.get("status", "?")
filled = order.get("fill_count", 0)
print(f"Order status: {status}, fill_count: {filled}")

bal_after = kt.get_balance()
print(f"Kalshi balance after: ${bal_after:.2f}")
print()
if filled and int(filled) >= units_to_buy:
    print("✓ Short fully closed.")
elif filled and int(filled) > 0:
    print(f"⚠ Partially closed: {filled}/{units_to_buy} filled.")
else:
    print("✗ Order not filled — position unchanged. Try a higher price.")

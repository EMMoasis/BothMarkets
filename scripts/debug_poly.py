#!/usr/bin/env python3
"""Debug Polymarket auth with different signature types."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs
from py_clob_client.order_builder.constants import BUY

CLOB_HOST = "https://clob.polymarket.com"
pk     = os.environ["POLY_PRIVATE_KEY"].strip()
ak     = os.environ["POLY_API_KEY"].strip()
sec    = os.environ["POLY_API_SECRET"].strip()
passph = os.environ["POLY_API_PASSPHRASE"].strip()
funder = os.environ.get("POLY_FUNDER", "").strip()

print(f"API key:  {ak}")
print(f"Funder:   {funder}")

creds = ApiCreds(api_key=ak, api_secret=sec, api_passphrase=passph)

def test_balance(sig_type, funder_arg=None, label=""):
    kwargs = {"host": CLOB_HOST, "chain_id": 137, "key": pk, "creds": creds, "signature_type": sig_type}
    if funder_arg:
        kwargs["funder"] = funder_arg
    try:
        c = ClobClient(**kwargs)
        bal = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        usdc = float(bal.get("balance", 0)) / 1_000_000
        print(f"  [{label}] Balance OK: ${usdc:.2f}")
        return c
    except Exception as e:
        print(f"  [{label}] FAILED: {e}")
        return None

print("\nTesting signature_type=0 (EOA, no funder):")
c0 = test_balance(0, funder_arg=None, label="sig_type=0")

print("\nTesting signature_type=2 (proxy, with funder):")
c2 = test_balance(2, funder_arg=funder, label="sig_type=2")

print("\nTesting signature_type=0 (EOA, WITH funder):")
c0f = test_balance(0, funder_arg=funder, label="sig_type=0+funder")

# If any worked, try placing a small order
working_client = c0 or c2 or c0f
if working_client:
    print("\nBalance call worked! Now testing order placement...")
    # Very small order: 1 contract of a known token
    # cs2-shin-bhe YES token from gamma
    import httpx, json
    r = httpx.get("https://gamma-api.polymarket.com/events", params={"slug": "cs2-shin-bhe-2026-02-26"}, timeout=10)
    markets = r.json()[0]["markets"]
    pm = markets[0]
    toks = json.loads(pm["clobTokenIds"])
    token_id = toks[1]  # NO token (Bounty Hunters wins)

    # Fetch current ask price
    rb = httpx.get(f"https://clob.polymarket.com/book", params={"token_id": token_id}, timeout=8)
    asks = rb.json().get("asks", [])
    if asks:
        best_ask = float(asks[-1]["price"])
        print(f"  Token: ...{token_id[-16:]}  best_ask={best_ask:.3f}")

        from py_clob_client.clob_types import OrderType
        try:
            order = working_client.create_order(OrderArgs(token_id=token_id, price=best_ask, size=1.0, side=BUY))
            resp = working_client.post_order(order, orderType=OrderType.FOK)
            print(f"  Order placed! orderID={resp.get('orderID','?')} status={resp.get('status','?')}")
            print(f"  Full resp: {json.dumps(resp, indent=2)}")
        except Exception as e:
            print(f"  Order FAILED: {e}")
    else:
        print("  No asks available for token")
else:
    print("\nAll balance calls failed. Need to re-create API keys.")

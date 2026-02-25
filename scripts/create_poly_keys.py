#!/usr/bin/env python3
"""
Recover or re-create Polymarket API credentials for the bothmarkets wallet.

Strategy:
  1. Try derive_api_key(nonce=N) for N in 0..4 — recovers existing registered keys
     without creating new ones. Test each with balance call.
  2. If none derived, try create_api_key(nonce=N) for N in 2..5 — registers new keys.
  3. When a working set is found, update .env automatically.
"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

CLOB_HOST = "https://clob.polymarket.com"
ENV_PATH  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

pk     = os.environ["POLY_PRIVATE_KEY"].strip()
funder = os.environ.get("POLY_FUNDER", "").strip() or None

print(f"Private key : {pk[:8]}...")
print(f"Funder addr : {funder}")


def test_creds(creds_obj, label=""):
    """Return USDC balance float if credentials work, else None."""
    for sig_type, use_funder in [(2, True), (0, True), (0, False)]:
        kwargs = dict(host=CLOB_HOST, chain_id=137, key=pk,
                      creds=creds_obj, signature_type=sig_type)
        if use_funder and funder:
            kwargs["funder"] = funder
        tag = f"sig={sig_type},funder={use_funder and bool(funder)}"
        try:
            c = ClobClient(**kwargs)
            bal = c.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            usdc = float(bal.get("balance", 0)) / 1_000_000
            print(f"  [{label}|{tag}] OK  balance=${usdc:.2f}")
            return usdc
        except Exception as e:
            print(f"  [{label}|{tag}] FAIL: {e}")
    return None


def build_eoa_client():
    """L1-only client for key creation/derivation — no creds needed."""
    return ClobClient(host=CLOB_HOST, chain_id=137, key=pk, signature_type=0)


def build_proxy_client():
    """L1 client as proxy operator — no creds needed."""
    if not funder:
        return None
    return ClobClient(host=CLOB_HOST, chain_id=137, key=pk,
                      signature_type=2, funder=funder)


def write_env(new_creds):
    with open(ENV_PATH, "r") as f:
        content = f.read()
    content = re.sub(r"^POLY_API_KEY=.*$",        f"POLY_API_KEY={new_creds.api_key}",        content, flags=re.M)
    content = re.sub(r"^POLY_API_SECRET=.*$",     f"POLY_API_SECRET={new_creds.api_secret}",     content, flags=re.M)
    content = re.sub(r"^POLY_API_PASSPHRASE=.*$", f"POLY_API_PASSPHRASE={new_creds.api_passphrase}", content, flags=re.M)
    with open(ENV_PATH, "w") as f:
        f.write(content)
    print(f"  .env updated: key={new_creds.api_key}")


# ---------------------------------------------------------------------------
# Phase 1: Derive existing keys (no registration — deterministic)
# ---------------------------------------------------------------------------
print("\n=== Phase 1: derive_api_key (recovering existing) ===")

for base_client_label, base_client in [("EOA", build_eoa_client()), ("proxy", build_proxy_client())]:
    if base_client is None:
        continue
    for nonce in range(5):
        label = f"derive nonce={nonce} via {base_client_label}"
        try:
            c = base_client.derive_api_key(nonce=nonce)
            print(f"\n  Derived [{label}]: key={c.api_key}")
            creds_obj = ApiCreds(api_key=c.api_key, api_secret=c.api_secret,
                                 api_passphrase=c.api_passphrase)
            bal = test_creds(creds_obj, label=label)
            if bal is not None:
                print(f"\nWORKING CREDENTIALS FOUND via {label}!")
                write_env(c)
                print("Done — .env updated. Run debug_poly.py to confirm.")
                sys.exit(0)
        except Exception as e:
            print(f"  derive [{label}] error: {e}")

# ---------------------------------------------------------------------------
# Phase 2: Create / register new keys (new nonces 2..5)
# ---------------------------------------------------------------------------
print("\n=== Phase 2: create_api_key (registering new) ===")

for base_client_label, base_client in [("EOA", build_eoa_client()), ("proxy", build_proxy_client())]:
    if base_client is None:
        continue
    for nonce in range(2, 6):
        label = f"create nonce={nonce} via {base_client_label}"
        try:
            c = base_client.create_api_key(nonce=nonce)
            print(f"\n  Created [{label}]: key={c.api_key}")
            creds_obj = ApiCreds(api_key=c.api_key, api_secret=c.api_secret,
                                 api_passphrase=c.api_passphrase)
            bal = test_creds(creds_obj, label=label)
            if bal is not None:
                print(f"\nWORKING CREDENTIALS FOUND via {label}!")
                write_env(c)
                print("Done — .env updated. Run debug_poly.py to confirm.")
                sys.exit(0)
        except Exception as e:
            print(f"  create [{label}] error: {e}")

print("\nAll attempts failed. Manual intervention required.")
print("Check: https://polymarket.com/profile?tab=api-keys — do you see any registered keys?")

#!/usr/bin/env python3
"""Debug Kalshi POST auth - try different signing variants."""
import os, sys, json, base64, time, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

api_key = os.environ["KALSHI_API_KEY"]
pem_raw = os.environ["KALSHI_API_SECRET"]
pem = pem_raw.strip().replace("\\n", "\n")
priv = serialization.load_pem_private_key(pem.encode(), password=None)

BASE   = "https://api.elections.kalshi.com/trade-api/v2"
PREFIX = "/trade-api/v2"

def sign(method, path, body, use_ms=True):
    if use_ms:
        ts = str(int(time.time() * 1000))
    else:
        ts = str(int(time.time()))
    msg = (ts + method.upper() + path + body).encode()
    sig = priv.sign(
        msg,
        asym_padding.PSS(mgf=asym_padding.MGF1(hashes.SHA256()), salt_length=asym_padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return ts, base64.b64encode(sig).decode()

http = httpx.Client(timeout=10, headers={"Accept": "application/json", "Content-Type": "application/json"}, follow_redirects=True)

body = {
    "ticker": "KXVALORANTMAP-26FEB24EPGX-1-EP",
    "client_order_id": str(uuid.uuid4()),
    "type": "limit",
    "action": "buy",
    "side": "yes",
    "count": 1,
    "yes_price": 47,
}
body_str = json.dumps(body, separators=(",", ":"))
path2 = "/portfolio/orders"

# Variant A: milliseconds + full path + body (current implementation)
ts, sig = sign("POST", PREFIX + path2, body_str, use_ms=True)
r = http.post(BASE + path2, content=body_str, headers={"KALSHI-ACCESS-KEY": api_key, "KALSHI-ACCESS-SIGNATURE": sig, "KALSHI-ACCESS-TIMESTAMP": ts})
print(f"Variant A (ms, full_path, body): {r.status_code}")
print(r.text[:300])
print()

# Variant B: seconds + full path + body
ts, sig = sign("POST", PREFIX + path2, body_str, use_ms=False)
r = http.post(BASE + path2, content=body_str, headers={"KALSHI-ACCESS-KEY": api_key, "KALSHI-ACCESS-SIGNATURE": sig, "KALSHI-ACCESS-TIMESTAMP": ts})
print(f"Variant B (sec, full_path, body): {r.status_code}")
print(r.text[:300])
print()

# Variant C: milliseconds + short path + body (path without /trade-api/v2)
ts, sig = sign("POST", path2, body_str, use_ms=True)
r = http.post(BASE + path2, content=body_str, headers={"KALSHI-ACCESS-KEY": api_key, "KALSHI-ACCESS-SIGNATURE": sig, "KALSHI-ACCESS-TIMESTAMP": ts})
print(f"Variant C (ms, short_path, body): {r.status_code}")
print(r.text[:300])
print()

# Variant D: milliseconds + full path + EMPTY body
ts, sig = sign("POST", PREFIX + path2, "", use_ms=True)
r = http.post(BASE + path2, content=body_str, headers={"KALSHI-ACCESS-KEY": api_key, "KALSHI-ACCESS-SIGNATURE": sig, "KALSHI-ACCESS-TIMESTAMP": ts})
print(f"Variant D (ms, full_path, empty body): {r.status_code}")
print(r.text[:300])
print()

# Variant E: GET /portfolio/orders (list) - should auth fine if signing is OK
ts, sig = sign("GET", PREFIX + "/portfolio/orders", "", use_ms=True)
r = http.get(BASE + "/portfolio/orders", headers={"KALSHI-ACCESS-KEY": api_key, "KALSHI-ACCESS-SIGNATURE": sig, "KALSHI-ACCESS-TIMESTAMP": ts})
print(f"Variant E GET /portfolio/orders: {r.status_code}")
print(r.text[:300])

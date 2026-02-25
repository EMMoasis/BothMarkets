#!/usr/bin/env python3
"""
scripts/test_trade.py — Connectivity test: places small live orders on Kalshi + Polymarket.

Uses known-good matched pairs (from the scanner's last live run) to fetch
current prices and place a small test trade.  Budget: up to $5 total.

Usage:
    python scripts/test_trade.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import httpx

from scanner.kalshi_trader import KalshiTrader
from scanner.poly_trader import PolyTrader

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE  = "https://gamma-api.polymarket.com"
CLOB_BASE   = "https://clob.polymarket.com"

MAX_BUDGET = 5.0
TEST_UNITS = 2   # fall back to 1 if needed


# ---------------------------------------------------------------------------
# Confirmed matched pairs from the scanner's last run (2026-02-24).
# Format: (kalshi_event_ticker, poly_slug, team_a_abbr, team_b_abbr)
# team_a = first team in Kalshi event ticker; team_b = second.
# Poly slug: team order may differ — we'll check outcomes to align.
# ---------------------------------------------------------------------------
KNOWN_PAIRS = [
    # CS2 matches closing 2026-02-26
    ("KXCS2MAP-26FEB26BHESHIN",  "cs2-shin-bhe-2026-02-26",   "BHE", "SHIN"),
    ("KXCS2MAP-26FEB26FOKBB",    "cs2-bb3-fokus-2026-02-26",  "FOK", "BB"),
    ("KXCS2MAP-26FEB26GLSIMP",   "cs2-imp11-gls1-2026-02-26", "GLS", "IMP"),
    ("KXCS2MAP-26FEB26YAWFLU",   "cs2-fxw7-yaw-2026-02-26",  "YAW", "FLU"),
    ("KXCS2MAP-26FEB26ICEFNC",   "cs2-fnc-ice-2026-02-26",   "ICE", "FNC"),
    ("KXCS2MAP-26FEB26GLSSHIN",  "cs2-shin-gls1-2026-02-26", "GLS2","SHIN2"),
    ("KXCS2MAP-26FEB26BHEYAW",   "cs2-yaw-bhe-2026-02-26",   "BHE2","YAW2"),
    # LOL matches closing 2026-02-25 and later
    ("KXLOLMAP-26FEB25TLNJL",    "lol-jl-tlnpir-2026-02-25", "TLN", "JL"),
    ("KXLOLMAP-26FEB27GZSHG",    "lol-shg-gz-2026-02-27",    "GZ",  "SHG"),
    ("KXLOLMAP-26FEB26FSKBOM",   "lol-bombat-fsk-2026-02-26","FSK", "BOM"),
    ("KXLOLMAP-26FEB26GLSSLY",   "lol-sly-gls-2026-02-26",   "GLS3","SLY"),
    ("KXLOLMAP-26FEB26DCGTSW",   "lol-tsw-dcg-2026-02-26",   "DCG", "TSW"),
    # VALORANT matches
    ("KXVALORANTMAP-26FEB24EXLBBB", "val-bbb-exl1-2026-02-24", "EXL","BBB"),
    ("KXVALORANTMAP-26FEB24SHI9Z",  "val-9z-shi-2026-02-24",   "SHIN3","9Z"),
    ("KXVALORANTMAP-26FEB24EPGX",   "val-gx-ep1-2026-02-24",   "EP",  "GX"),
    ("KXVALORANTMAP-26FEB24UCAMULF","val-ulf-ucam-2026-02-24", "UCAM","ULF"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def kalshi_get_event_markets(event_ticker: str, http: httpx.Client) -> list[dict]:
    """Fetch all Kalshi markets that belong to a given event_ticker."""
    try:
        r = http.get(
            f"{KALSHI_BASE}/markets",
            params={"status": "open", "event_ticker": event_ticker, "limit": 20},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("markets", [])
    except Exception as e:
        print(f"    kalshi event {event_ticker} failed: {e}")
        return []


def gamma_event_markets(slug: str, http: httpx.Client) -> list[dict]:
    """Return enriched Gamma markets for an event slug."""
    try:
        r = http.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        events = r.json()
        if not events:
            return []
        result = []
        for m in events[0].get("markets", []):
            raw_tok = m.get("clobTokenIds", "[]")
            raw_out = m.get("outcomes", '["Yes","No"]')
            try:
                toks = json.loads(raw_tok) if isinstance(raw_tok, str) else raw_tok
            except Exception:
                toks = []
            try:
                outs = json.loads(raw_out) if isinstance(raw_out, str) else raw_out
            except Exception:
                outs = ["Yes", "No"]
            result.append({
                "question": m.get("question", ""),
                "token_ids": toks,
                "outcomes": outs,
                "sort": m.get("sortIndex", 0),
            })
        result.sort(key=lambda x: x["sort"])
        return result
    except Exception as e:
        print(f"    gamma slug {slug} failed: {e}")
        return []


def clob_best_ask(token_id: str, http: httpx.Client) -> tuple[float | None, float]:
    """(best_ask_cents, depth). Asks sorted descending → best = last."""
    try:
        r = http.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=8)
        r.raise_for_status()
        asks = r.json().get("asks", [])
        if not asks:
            return None, 0.0
        best = asks[-1]
        return round(float(best["price"]) * 100, 2), float(best.get("size", 0))
    except Exception:
        return None, 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    http = httpx.Client(timeout=15, follow_redirects=True)

    k_trader = KalshiTrader(
        api_key=os.environ["KALSHI_API_KEY"],
        api_secret_pem=os.environ["KALSHI_API_SECRET"],
    )
    p_trader = PolyTrader(
        private_key=os.environ["POLY_PRIVATE_KEY"],
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
        funder=os.environ.get("POLY_FUNDER"),
    )

    print(f"Kalshi balance:     ${k_trader.get_balance():.2f}")
    print(f"Polymarket balance: ${p_trader.get_usdc_balance():.2f}")
    print()

    best: dict | None = None

    for event_ticker, poly_slug, ta, tb in KNOWN_PAIRS:
        print(f"Checking {event_ticker}  |  {poly_slug}")

        # ── Kalshi ──
        time.sleep(1.5)  # rate limit
        k_markets = kalshi_get_event_markets(event_ticker + "-1", http)
        if not k_markets:
            # Try just base event ticker
            k_markets = kalshi_get_event_markets(event_ticker, http)
        if not k_markets:
            print(f"  Kalshi: no markets found")
            continue

        # Group: we want two team markets for the same map
        map1_markets = [m for m in k_markets if m.get("event_ticker", "").endswith("-1")]
        if len(map1_markets) < 2:
            map1_markets = k_markets[:2]

        if len(map1_markets) < 2:
            print(f"  Kalshi: only {len(k_markets)} markets (need 2 for a map pair)")
            continue

        m_a = map1_markets[0]
        m_b = map1_markets[1]
        ya_ask = m_a.get("yes_ask")
        yb_ask = m_b.get("yes_ask")
        team_a_name = m_a.get("yes_sub_title") or ta
        team_b_name = m_b.get("yes_sub_title") or tb
        tk_a = m_a.get("ticker", "")
        tk_b = m_b.get("ticker", "")

        print(f"  Kalshi: {team_a_name}({ya_ask}c) vs {team_b_name}({yb_ask}c)")

        if ya_ask is None and yb_ask is None:
            print(f"  Kalshi: no prices available")
            continue

        # ── Polymarket ──
        poly_markets = gamma_event_markets(poly_slug, http)
        if not poly_markets:
            print(f"  Poly: no markets for slug {poly_slug}")
            continue

        pm = poly_markets[0]  # first game/map
        toks = pm["token_ids"]
        if len(toks) < 2:
            print(f"  Poly: only {len(toks)} tokens")
            continue

        yes_tok, no_tok = toks[0], toks[1]
        p_yes_ask, p_yes_sz = clob_best_ask(yes_tok, http)
        p_no_ask,  p_no_sz  = clob_best_ask(no_tok,  http)

        print(f"  Poly:   YES({p_yes_ask}c,sz={p_yes_sz}) NO({p_no_ask}c,sz={p_no_sz})")
        print(f"  Poly Q: {pm['question'][:70]}")

        # Determine token alignment.
        # Poly outcomes[0] = YES outcome. Compare to Kalshi team names.
        outcomes = pm["outcomes"]
        first_out = (outcomes[0] if outcomes else "").lower()
        ta_low = team_a_name.lower()
        tb_low = team_b_name.lower()

        # Figure out which Kalshi team aligns with which Poly token
        if any(w in first_out for w in ta_low.split()):
            # Poly YES = team_a wins
            p_a_tok, p_a_ask, p_a_sz = yes_tok, p_yes_ask, p_yes_sz
            p_b_tok, p_b_ask, p_b_sz = no_tok,  p_no_ask,  p_no_sz
        elif any(w in first_out for w in tb_low.split()):
            # Poly YES = team_b wins
            p_a_tok, p_a_ask, p_a_sz = no_tok,  p_no_ask,  p_no_sz
            p_b_tok, p_b_ask, p_b_sz = yes_tok, p_yes_ask, p_yes_sz
        else:
            # Can't determine alignment — try both and pick cheaper
            p_a_tok, p_a_ask, p_a_sz = yes_tok, p_yes_ask, p_yes_sz
            p_b_tok, p_b_ask, p_b_sz = no_tok,  p_no_ask,  p_no_sz
            print(f"  Warning: can't align teams. Guessing YES={team_a_name}, NO={team_b_name}.")

        # Evaluate strategies for TEST_UNITS and 1 unit
        for units in (TEST_UNITS, 1):
            # Strat A: Buy Kalshi team_a + Poly team_b token (hedged)
            if ya_ask and p_b_ask and p_b_sz >= units:
                combined = ya_ask + p_b_ask
                cost = units * combined / 100
                print(f"  Strat A: K-{team_a_name}({ya_ask}c)+P-{team_b_name}({p_b_ask}c)={combined:.1f}c x{units}=${cost:.2f}")
                if cost <= MAX_BUDGET and (best is None or combined < best["combined"]):
                    best = {
                        "desc":     f"{event_ticker} map1",
                        "units":    units, "strategy": "A",
                        "k_ticker": tk_a,  "k_side": "yes", "k_price": int(round(ya_ask)),
                        "p_token":  p_b_tok, "p_price": (p_b_ask or 0) / 100,
                        "combined": combined, "total_usd": cost,
                        "k_team":   team_a_name, "p_outcome": team_b_name,
                    }

            # Strat B: Buy Kalshi team_b + Poly team_a token (hedged)
            if yb_ask and p_a_ask and p_a_sz >= units:
                combined = yb_ask + p_a_ask
                cost = units * combined / 100
                print(f"  Strat B: K-{team_b_name}({yb_ask}c)+P-{team_a_name}({p_a_ask}c)={combined:.1f}c x{units}=${cost:.2f}")
                if cost <= MAX_BUDGET and (best is None or combined < best["combined"]):
                    best = {
                        "desc":     f"{event_ticker} map1",
                        "units":    units, "strategy": "B",
                        "k_ticker": tk_b,  "k_side": "yes", "k_price": int(round(yb_ask)),
                        "p_token":  p_a_tok, "p_price": (p_a_ask or 0) / 100,
                        "combined": combined, "total_usd": cost,
                        "k_team":   team_b_name, "p_outcome": team_a_name,
                    }

        print()

    # ── Done scanning ──
    if best is None:
        print("=" * 60)
        print("No tradable pair found within $5 budget.")
        print("Combined prices are all above $5 for 1-2 contracts, or Poly has no liquidity.")
        print("The background scanner will auto-execute when genuine arb appears.")
        return

    print("=" * 60)
    print(f"SELECTED: {best['desc']}")
    print(f"  Strategy {best['strategy']}: Kalshi {best['k_team']} YES @{best['k_price']}c")
    print(f"  Polymarket {best['p_outcome']} token @{best['p_price']*100:.1f}c")
    print(f"  Units: {best['units']}  Combined: {best['combined']:.1f}c  Total: ${best['total_usd']:.2f}")
    print()

    # ── Leg 1: Kalshi ──
    print(f">>> KALSHI: BUY yes x{best['units']} @{best['k_price']}c on {best['k_ticker']}")
    try:
        k_resp = k_trader.place_order(
            ticker=best["k_ticker"],
            side="yes",
            count=best["units"],
            price_cents=best["k_price"],
            action="buy",
        )
        k_oid    = k_resp.get("order", {}).get("order_id", "?")
        k_status = k_resp.get("order", {}).get("status", "?")
        print(f"    order_id : {k_oid}")
        print(f"    status   : {k_status}")
        print(json.dumps(k_resp, indent=4))
    except Exception as exc:
        print(f"    FAILED: {exc}")
        print("    Aborting — Polymarket leg NOT placed.")
        return

    time.sleep(0.5)

    # ── Leg 2: Polymarket ──
    print(f">>> POLY: BUY x{best['units']} token=...{best['p_token'][-16:]} @{best['p_price']:.4f}")
    try:
        p_resp = p_trader.place_order(
            token_id=best["p_token"],
            price=best["p_price"],
            size=float(best["units"]),
            side="BUY",
        )
        p_oid    = p_resp.get("orderID", "?")
        p_status = p_resp.get("status", "?")
        print(f"    orderID  : {p_oid}")
        print(f"    status   : {p_status}")
        print(json.dumps(p_resp, indent=4))
    except Exception as exc:
        print(f"    FAILED: {exc}")

    print()
    print("Test trade complete.")
    print(f"Final Kalshi balance:     ${k_trader.get_balance():.2f}")
    print(f"Final Polymarket balance: ${p_trader.get_usdc_balance():.2f}")


if __name__ == "__main__":
    main()

# BothMarkets — Cross-Platform Prediction Market Arbitrage Scanner

## What This Does

Scans Kalshi and Polymarket every 2 seconds for live prices on matched binary markets.
Market lists are refreshed every 2 hours.
Finds cross-platform arbitrage: same event on both platforms where combined YES+NO cost < 100c.
Currently SCAN-ONLY — no live trading.

## Arbitrage Logic

Buy Kalshi YES + Polymarket NO (Strategy A), or Kalshi NO + Polymarket YES (Strategy B).
Both pay 100c if the event resolves either way — guaranteed profit when combined cost < 100c.

Strategy A: kalshi_yes_ask + poly_no_ask < 100  → profit = 100 - combined
Strategy B: kalshi_no_ask + poly_yes_ask < 100  → profit = 100 - combined

## Market Matching — ALL 4 Must Match

1. Exact same asset (e.g., "BTC")
2. Exact same resolution date AND time (±1 hour tolerance)
3. Same resolution direction ("ABOVE" or "BELOW")
4. Exact same numeric threshold (after normalization: $90k = 90000)

If any criterion fails → no match.

## Profit Tiers (adjusted +0.5c for transfer fees)

- Ultra High: > 5.5c
- High:       2.5–5.5c
- Mid:        1.5–2.5c
- Low:        0.8–1.5c
- Below 0.8c: ignored

## Key Architecture Decisions

1. Two-speed loop: market list refresh every 2h (slow), price poll every 2s (fast).
2. Polymarket CLOB provides direct YES and NO token orderbooks — use both directly.
   CLOB bids sorted ASCENDING (best bid = bids[-1]), asks sorted DESCENDING (best ask = asks[-1]).
3. Kalshi prices are integer cents from REST API (no SDK needed for reads).
4. py-clob-client used for Polymarket CLOB price fetching.
5. All 4 market matching criteria are required — no fuzzy matching.

## Entry Point

python -m scanner.runner

## API Reference

Kalshi:    https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000
Gamma:     https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500
CLOB:      https://clob.polymarket.com/book?token_id=<id>

## Output Files

scanner.log         — all logs (debug + info)
opportunities.log   — filtered: only matched pairs and arb opportunities
opportunities.json  — NDJSON, one object per scan run

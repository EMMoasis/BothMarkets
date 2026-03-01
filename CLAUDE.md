# BothMarkets — Cross-Platform Prediction Market Arbitrage Scanner

## What This Does

Scans Kalshi and Polymarket every 2 seconds for live prices on matched binary markets.
Market lists are refreshed every 2 hours.
Finds cross-platform arbitrage: same event on both platforms where combined YES+NO cost < 100c.
Execution layer is LIVE — runs with `--live` flag, paper trading with `--paper` flag.

## Arbitrage Logic

Buy Kalshi YES + Polymarket NO (Strategy A), or Kalshi NO + Polymarket YES (Strategy B).
Both pay 100c if the event resolves either way — guaranteed profit when combined cost < 100c.

    Strategy A: kalshi_yes_ask + poly_no_ask < 100  → profit = 100 - combined
    Strategy B: kalshi_no_ask + poly_yes_ask < 100  → profit = 100 - combined

## Supported Market Types

### SPORTS (active)
Esports and traditional sports game-winner markets:

**Esports:** CS2, LOL (League of Legends), VALORANT, DOTA2, Rocket League
**US Sports:** NBA, WNBA, NFL, NHL, MLB
**College Sports:** NCAAB, NCAAF
**Soccer:** SOCCER (MLS, Premier League, Champions League, La Liga, Bundesliga, etc.)
**Cricket:** CRICKET (IPL, ICC, BBL, PSL, CPL)
**Tennis:** TENNIS (ATP, WTA, Wimbledon, Grand Slams)
**Golf:** GOLF (PGA Tour, LIV Golf, Masters, Ryder Cup)
**MMA / Combat:** MMA (UFC), BOXING
**Rugby:** RUGBY (NRL, Super Rugby, Six Nations, Rugby League/Union)
**Formula 1:** F1
**Other:** CFL, AFL, TABLE_TENNIS, LACROSSE

Kalshi tickers: KXCS2MAP-*, KXLOLMAP-*, KXVALORANTMAP-*, KXNBAWIN-*, KXCRICKET-*, KXIPL-*, KXTENNIS-*, KXATP-*, KXPGA-*, KXUFC-*, KXRUGBY-*, KXF1-*, etc.
Polymarket slugs: moneyline (series winner) and child_moneyline (per-map/game winner)

### CRYPTO (disabled — see below)
BTC, ETH, SOL, XRP, DOGE price-level markets.
Disabled by default due to oracle mismatch (see config.CRYPTO_MATCHING_ENABLED).

## Active Kalshi Sports (as of March 2026)

Kalshi currently only offers **esports** game-winner markets within a 72h window:
- CS2 (KXCS2MAP, KXCS2GAME), LOL (KXLOLMAP, KXLOLGAME), VALORANT (KXVALORANTMAP, KXVALORANTGAME), DOTA2 (KXDOTA2GAME)

Traditional sports NOT currently on Kalshi (within 72h): NBA, NHL, MLB, NFL, soccer, cricket, tennis.
- IPL cricket markets exist (KXIPL) but with June 2026 resolution dates — outside 72h window
- KXBOXING exists but typical fight dates are 4-5 days out (outside 72h)
- KXNCAAF and KXF1 have championship markets far in the future (months away)

Polymarket HAS NHL/NBA/NCAAB game-winner markets within 72h. No cross-platform match is possible
until Kalshi lists equivalent near-term markets.

## Sports Market Matching — ALL 6 Must Pass

1. **sport**       — same sport code (CS2, LOL, VALORANT, NBA, etc.)
2. **team**        — same normalized team name (see normalize_team_name below)
3. **opponent**    — same normalized opponent — prevents DRX vs TeamA matching DRX vs TeamB
                     when the same team plays multiple games in the 72h window
4. **date**        — resolution_dt within ±4 hours (RESOLUTION_TIME_TOLERANCE_HOURS_SPORTS)
                     4h tolerance because esports BO3/BO5 can run 4-6h; Kalshi uses
                     scheduled start time, Polymarket uses expected end time — gap can be 3-5h
5. **subtype**     — "map" (per-map/game winner) vs "series" (match/series winner)
                     prevents KXLOLMAP map-winner matching Polymarket series-winner
6. **map_number**  — when both markets specify a game/map number (e.g. "map 2" vs "Game 3"),
                     they must match. Skipped if either market has no map number.

If any criterion fails → pair is rejected. Rejection reasons are logged.

## Team Name Normalization (normalize_team_name)

Applied to all team names before matching:
- Lowercase, remove punctuation
- Strip common wrapper words: "team", "esports", "gaming", "fc", "sc", "the"
  (does NOT strip team identifiers like "g2", "m80", etc.)
- Guard: if stripping all words leaves empty string, keep originals
- Strip trailing numbers: "Cloud9 2" → "cloud9"
- Examples: "G2 Esports" → "g2", "Team Vitality" → "vitality", "M80" → "m80"

## Map Number Extraction (_extract_map_number)

Regex: `\b(?:map|game)\s+(\d+)\b` (case-insensitive)
- Kalshi: "Will X win map 2 in the match?" → map_number=2
- Polymarket: "CS2: X vs Y - Map 1 Winner" → map_number=1
- Polymarket: "LoL: X vs Y - Game 3 Winner" → map_number=3
- Does NOT match: "Will over 2.5 maps be played?" (requires "map"/"game" + immediate digit)

## Crypto Matching — DISABLED

Disabled via `CRYPTO_MATCHING_ENABLED = False` in config.py.

**Why disabled:**
- Kalshi resolves via CF Benchmarks BRTI (60-sec multi-exchange average)
- Polymarket resolves via Binance 1-min candle BTC/USDT close
- Different oracles → a covered position (YES one, NO other) is NOT risk-free
- Structural 5-hour gap: Kalshi closes at 5pm ET, Polymarket closes at 12pm ET
- Different price ladders: Kalshi dynamically clusters around spot, Poly uses fixed $100 increments
- Result: no overlapping thresholds + date gap → 0 matches even when enabled

**When crypto is enabled, 4 criteria are checked:**
1. asset — same coin (BTC, ETH, etc.)
2. direction — same direction (ABOVE/BELOW)
3. date — resolution within ±1h
4. threshold — exact same dollar amount (e.g. 90000.0)

**Kalshi crypto field structure (important):**
- `title`: "Bitcoin price on Feb 24, 2026?" — asset name lives here
- `subtitle`: "$75,750 or above" — direction + threshold live here
- Fix: _normalize_crypto combines title+subtitle for parsing

## Profit Tiers (adjusted +0.5c for transfer fees)

- Ultra High: > 5.5c spread
- High:       2.5–5.5c
- Mid:        1.5–2.5c
- Low:        0.8–1.5c
- Below 0.8c: ignored (MIN_SPREAD_CENTS)

## Architecture

### Two-Speed Loop
- Market list refresh: every 2 hours (slow) — fetches all open markets, normalizes, matches pairs
- Price poll: every 2 seconds (fast) — fetches live prices for matched pairs, checks for arb

### Kalshi Data Flow
1. Paginate GET /markets?status=open&limit=1000 (all pages, ~250+ pages)
2. `_normalize_one`: classify as SPORTS (series_ticker lookup) or CRYPTO (keyword parsing)
3. Sports: extract team, opponent from yes_sub_title + title ("Will X win the X vs. Y match?")
4. Crypto: combine title + subtitle to extract asset, direction, threshold
5. Filter by 72h window (resolution_dt ≤ now+72h)

### Polymarket Data Flow
1. Gamma API: GET /markets?active=true&closed=false (paginated)
2. Classify by question text: crypto keywords (bitcoin/eth) vs sports (CS2/LOL/team names)
3. Sports: expand each binary market into TWO NormalizedMarket entries (one per team)
   - team entry: yes_token_id = that team's win token, no_token_id = opponent's win token
4. Crypto: extract asset, direction, threshold from question text
5. Fetch live prices via CLOB GET /book?token_id=<id> for each matched pair

### Matching Index
Sports markets are pre-indexed by (sport, team, sport_subtype) for O(1) lookup.
Within each bucket, all 6 criteria are checked against each candidate.
Each Kalshi market and each Polymarket market appears in at most one matched pair.

### Price Fetching
- Kalshi: GET /markets/{ticker} (price) + GET /markets/{ticker}/orderbook (depth)
- Polymarket: GET /clob.polymarket.com/book?token_id={id} for YES and NO tokens separately
  CLOB asks are sorted DESCENDING → best ask = asks[0] (NOT asks[-1])
  Depth = sum of contracts at best ask price level

## Key Files

| File | Purpose |
|------|---------|
| scanner/runner.py | Main loop (two-speed: refresh + poll) |
| scanner/kalshi_client.py | Kalshi market fetch + normalization + price polling |
| scanner/poly_client.py | Polymarket Gamma fetch + CLOB price polling |
| scanner/market_matcher.py | Cross-platform matching (6-criteria sports, 4-criteria crypto) |
| scanner/opportunity_finder.py | Arb detection from matched pair prices |
| scanner/models.py | NormalizedMarket, MatchedPair, Opportunity dataclasses |
| scanner/config.py | All constants (RESOLUTION_TIME_TOLERANCE_HOURS, CRYPTO_MATCHING_ENABLED, etc.) |

## Entry Point

    python -m scanner.runner           # scan-only (no trades)
    python -m scanner.runner --live    # live trading
    python -m scanner.runner --paper   # paper trading (dry-run, logs trades but no real orders)

## API Endpoints

    Kalshi market list:  https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000
    Kalshi market price: https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}
    Kalshi orderbook:    https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook
    Gamma (Poly list):   https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500
    CLOB (Poly prices):  https://clob.polymarket.com/book?token_id={token_id}

## Output Files

    scanner.log          — all logs (debug + info)
    opportunities.log    — filtered: only matched pairs and arb opportunities
    opportunities.json   — NDJSON, one object per scan run

## Environment Variables (credentials for trading)

    KALSHI_API_KEY       — Kalshi RSA API key ID
    KALSHI_API_SECRET    — Kalshi RSA private key (PEM)
    POLY_PRIVATE_KEY     — Polymarket Ethereum private key (0x...)
    POLY_API_KEY         — Polymarket CLOB API key
    POLY_API_SECRET      — Polymarket CLOB API secret
    POLY_API_PASSPHRASE  — Polymarket CLOB API passphrase
    POLY_FUNDER          — Polymarket funder wallet address

## Known Limitations / Edge Cases

- Same Kalshi event_ticker used for both team perspectives → same platform_url displayed
  for both pairs of a match (URL shows event, not individual contract). platform_id is unique.
- Polymarket "None" prices: CLOB returns no ask when a token has no open orders.
- Kalshi maps 1/2/3 of a series all share the same resolution_dt → map_number is the
  only way to distinguish them (date tolerance alone is insufficient).
- Soccer/non-esports Polymarket markets may parse team names as "yes"/"no" if the question
  format doesn't match the sports regex. These fail to match Kalshi and are benign.
- 327/327 tests passing (pytest tests/).
- Polymarket soccer "Will X win?" (YES/NO outcomes) are handled by `_normalize_yes_no_sports_market`:
  extracts team name from question text, skips draw markets.
- Date tolerance for sports is 4h (RESOLUTION_TIME_TOLERANCE_HOURS_SPORTS), separate from crypto (1h).

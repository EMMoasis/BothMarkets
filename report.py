"""Paper run report — reads scanner_paper.db and prints a full breakdown."""
import sqlite3

db = sqlite3.connect("scanner_paper.db")
db.row_factory = sqlite3.Row

# ── Overview ──────────────────────────────────────────────────────────────────
ov = db.execute("""
SELECT
    COUNT(*)                                                   AS total_trades,
    SUM(CASE WHEN status='filled'       THEN 1 ELSE 0 END)    AS filled,
    SUM(CASE WHEN status='partial_stuck' THEN 1 ELSE 0 END)   AS partial_stuck,
    SUM(CASE WHEN status='skipped'      THEN 1 ELSE 0 END)    AS skipped,
    SUM(CASE WHEN status='unwound'      THEN 1 ELSE 0 END)    AS unwound,
    SUM(CASE WHEN status='error'        THEN 1 ELSE 0 END)    AS errors,
    SUM(CASE WHEN status='filled' THEN locked_profit_usd ELSE 0 END) AS gross_profit,
    SUM(CASE WHEN status='filled' THEN net_profit_usd    ELSE 0 END) AS net_profit,
    SUM(CASE WHEN status='filled' THEN total_cost_usd    ELSE 0 END) AS capital_deployed,
    SUM(CASE WHEN status='filled' THEN kalshi_fee_usd    ELSE 0 END) AS total_fees,
    MIN(traded_at) AS first_trade,
    MAX(traded_at) AS last_trade
FROM trades
""").fetchone()

print("=" * 68)
print("PAPER RUN REPORT — BothMarkets Scanner")
print("=" * 68)
print(f"  Period            : {ov['first_trade']} -> {ov['last_trade']}")
print(f"  Total trades      : {ov['total_trades']}")
print(f"  Filled            : {ov['filled']}")
print(f"  Partial-stuck     : {ov['partial_stuck']}")
print(f"  Skipped           : {ov['skipped']}")
print(f"  Unwound           : {ov['unwound']}")
print(f"  Errors            : {ov['errors']}")
print()
print(f"  Gross profit      : ${ov['gross_profit']:.4f}")
print(f"  Kalshi fees       : ${ov['total_fees']:.4f}")
print(f"  Net profit        : ${ov['net_profit']:.4f}")
print(f"  Capital deployed  : ${ov['capital_deployed']:.2f}")
if ov['capital_deployed']:
    roi = ov['net_profit'] / ov['capital_deployed'] * 100
    print(f"  ROI               : {roi:.2f}%")

# ── By tier ───────────────────────────────────────────────────────────────────
print()
print("BY TIER (filled trades only)")
print("-" * 68)
rows = db.execute("""
SELECT
    o.tier,
    COUNT(*) AS trades,
    AVG(o.spread_cents) AS avg_spread,
    SUM(t.locked_profit_usd) AS gross,
    SUM(t.net_profit_usd)    AS net,
    AVG(t.locked_profit_usd) AS avg_profit,
    SUM(t.total_cost_usd)    AS capital
FROM trades t
JOIN opportunities o ON t.opportunity_id = o.id
WHERE t.status = 'filled'
GROUP BY o.tier
ORDER BY SUM(t.net_profit_usd) DESC
""").fetchall()
print(f"  {'Tier':<15} {'Trades':>7} {'AvgSprd':>8} {'Gross':>10} {'Net':>10} {'AvgProfit':>10} {'Capital':>10}")
for r in rows:
    print(f"  {(r['tier'] or '?'):<15} {r['trades']:>7} {r['avg_spread']:>7.2f}c"
          f" {r['gross']:>9.2f} {r['net']:>9.2f} {r['avg_profit']:>9.4f} {r['capital']:>9.2f}")

# ── By sport ──────────────────────────────────────────────────────────────────
print()
print("BY SPORT (filled trades only)")
print("-" * 68)
rows = db.execute("""
SELECT
    CASE
        WHEN kalshi_ticker LIKE 'KXNBAGAME%' THEN 'NBA'
        WHEN kalshi_ticker LIKE 'KXNHLGAME%' THEN 'NHL'
        WHEN kalshi_ticker LIKE 'KXMLBGAME%' THEN 'MLB'
        WHEN kalshi_ticker LIKE 'KXNFLGAME%' THEN 'NFL'
        WHEN kalshi_ticker LIKE 'KXCS%'      THEN 'CS2/CSGO'
        WHEN kalshi_ticker LIKE 'KXLOL%'     THEN 'LoL'
        WHEN kalshi_ticker LIKE 'KXDOTA%'    THEN 'DOTA2'
        WHEN kalshi_ticker LIKE 'KXVAL%'     THEN 'Valorant'
        WHEN kalshi_ticker LIKE 'KXRL%'      THEN 'RocketLeague'
        ELSE 'Other'
    END AS sport,
    COUNT(*) AS trades,
    SUM(locked_profit_usd) AS gross,
    SUM(net_profit_usd)    AS net,
    AVG(net_profit_usd)    AS avg_net,
    SUM(total_cost_usd)    AS capital
FROM trades
WHERE status = 'filled'
GROUP BY sport
ORDER BY net DESC
""").fetchall()
print(f"  {'Sport':<14} {'Trades':>7} {'Gross':>10} {'Net':>10} {'AvgNet':>10} {'Capital':>10}")
for r in rows:
    print(f"  {r['sport']:<14} {r['trades']:>7} {r['gross']:>9.2f} {r['net']:>9.2f}"
          f" {r['avg_net']:>9.4f} {r['capital']:>9.2f}")

# ── Skip reasons ──────────────────────────────────────────────────────────────
print()
print("SKIP REASONS")
print("-" * 68)
rows = db.execute("""
SELECT reason, COUNT(*) AS cnt
FROM trades
WHERE status = 'skipped'
GROUP BY reason
ORDER BY cnt DESC
LIMIT 20
""").fetchall()
for r in rows:
    print(f"  {r['cnt']:>6}  {r['reason']}")

# ── Top 15 most profitable single trades ─────────────────────────────────────
print()
print("TOP 15 SINGLE TRADES (by gross locked profit)")
print("-" * 68)
rows = db.execute("""
SELECT kalshi_ticker, kalshi_side, poly_side,
       requested_units, kalshi_price_cents, poly_price_cents,
       locked_profit_usd, net_profit_usd, traded_at
FROM trades
WHERE status = 'filled'
ORDER BY locked_profit_usd DESC
LIMIT 15
""").fetchall()
for r in rows:
    ticker = r['kalshi_ticker'][:38]
    print(f"  {ticker:<38} K={r['kalshi_side']} P={r['poly_side']}"
          f" {r['requested_units']}u @{r['kalshi_price_cents']}+{r['poly_price_cents']}c"
          f" gross=${r['locked_profit_usd']:.2f} net=${r['net_profit_usd']:.2f}")

# ── Opportunity funnel ────────────────────────────────────────────────────────
print()
print("OPPORTUNITY FUNNEL")
print("-" * 68)
opp_total   = db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
opp_with_fill = db.execute(
    "SELECT COUNT(DISTINCT opportunity_id) FROM trades WHERE status='filled'"
).fetchone()[0]
print(f"  Total opportunities detected : {opp_total}")
print(f"  Opportunities with a fill    : {opp_with_fill}")
if opp_total:
    print(f"  Conversion rate              : {opp_with_fill/opp_total*100:.1f}%")

# ── Daily breakdown ───────────────────────────────────────────────────────────
print()
print("DAILY BREAKDOWN (filled trades)")
print("-" * 68)
rows = db.execute("""
SELECT
    substr(traded_at, 1, 10) AS day,
    COUNT(*) AS trades,
    SUM(locked_profit_usd) AS gross,
    SUM(net_profit_usd) AS net,
    SUM(total_cost_usd) AS capital
FROM trades
WHERE status = 'filled'
GROUP BY day
ORDER BY day
""").fetchall()
for r in rows:
    print(f"  {r['day']}  trades={r['trades']:>4}  gross=${r['gross']:>8.2f}"
          f"  net=${r['net']:>8.2f}  capital=${r['capital']:>9.2f}")

db.close()

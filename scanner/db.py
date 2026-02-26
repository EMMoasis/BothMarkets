"""SQLite persistence for opportunity and trade logging.

Two tables:
  opportunities  — every arb opportunity the scanner detects (traded or not)
  trades         — every trade execution attempt with full cost/profit detail
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from scanner.config import KALSHI_TAKER_FEE_RATE
from scanner.models import Opportunity

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_db(path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite database and ensure tables exist."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads without blocking
    conn.execute("PRAGMA foreign_keys=ON")
    _create_tables(conn)
    _migrate(conn)
    log.info("DB | Initialized at %s", path)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add new columns to existing DBs without breaking old data."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
    migrations = [
        ("kalshi_fee_usd", "REAL"),
        ("net_profit_usd", "REAL"),
    ]
    for col, typ in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
            log.info("DB | Migration: added column trades.%s", col)
    # Backfill fee and net profit for existing filled rows
    if "kalshi_fee_usd" not in existing:
        conn.execute("""
            UPDATE trades
            SET kalshi_fee_usd = ROUND(kalshi_filled * ?, 4),
                net_profit_usd = ROUND(locked_profit_usd - (kalshi_filled * ?), 4)
            WHERE status = 'filled' AND kalshi_filled IS NOT NULL
        """, (KALSHI_TAKER_FEE_RATE, KALSHI_TAKER_FEE_RATE))
        log.info("DB | Migration: backfilled kalshi_fee_usd and net_profit_usd for existing rows")
    conn.commit()


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at             TEXT    NOT NULL,       -- ISO-8601 UTC
            kalshi_ticker          TEXT    NOT NULL,
            poly_token_id          TEXT    NOT NULL,
            kalshi_title           TEXT,                   -- raw market question
            poly_title             TEXT,
            strategy               TEXT,                   -- 'A' (K-YES+P-NO) or 'B' (K-NO+P-YES)
            kalshi_side            TEXT,                   -- 'YES' or 'NO'
            poly_side              TEXT,                   -- 'YES' or 'NO'
            kalshi_cost_cents      REAL,                   -- price paid for Kalshi leg
            poly_cost_cents        REAL,                   -- price paid for Poly leg
            spread_cents           REAL,                   -- guaranteed profit per share
            tier                   TEXT,                   -- Low / Mid / High / Ultra High
            kalshi_depth_contracts REAL,                   -- contracts available at that ask
            poly_depth_shares      REAL,                   -- shares available at that ask
            tradeable_units        INTEGER,                -- min(k_depth, p_depth)
            max_locked_profit_usd  REAL,                   -- tradeable_units * spread / 100
            hours_to_close         REAL,                   -- hours until earlier market closes
            kalshi_close_time      TEXT,                   -- ISO-8601 UTC
            poly_close_time        TEXT,                   -- ISO-8601 UTC
            executed               INTEGER DEFAULT 0       -- 0 = scan only, 1 = trade attempted
        );

        CREATE TABLE IF NOT EXISTS trades (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id        INTEGER REFERENCES opportunities(id),
            traded_at             TEXT    NOT NULL,        -- ISO-8601 UTC
            kalshi_ticker         TEXT    NOT NULL,
            poly_token_id         TEXT    NOT NULL,
            kalshi_side           TEXT,                    -- 'YES' or 'NO'
            poly_side             TEXT,                    -- 'YES' or 'NO'
            requested_units       INTEGER,                 -- units calculated before placing order
            kalshi_filled         INTEGER,                 -- actual Kalshi contracts filled
            poly_filled           INTEGER,                 -- actual Poly shares filled
            kalshi_price_cents    REAL,                    -- price at time of opportunity
            poly_price_cents      REAL,
            kalshi_cost_usd       REAL,                    -- USD spent on Kalshi leg
            poly_cost_usd         REAL,                    -- USD spent on Poly leg
            total_cost_usd        REAL,
            locked_profit_usd     REAL,                    -- guaranteed profit before fees (0 if not filled)
            kalshi_fee_usd        REAL,                    -- Kalshi taker fee (1.75% of face value)
            net_profit_usd        REAL,                    -- locked_profit_usd - kalshi_fee_usd
            kalshi_order_id       TEXT,
            poly_order_id         TEXT,
            status                TEXT,                    -- filled/skipped/unwound/partial_stuck/error
            reason                TEXT,                    -- skip/fail reason code
            kalshi_balance_before REAL,                    -- Kalshi cash before trade
            poly_balance_before   REAL                     -- Poly USDC before trade
        );

        CREATE INDEX IF NOT EXISTS idx_opp_scanned_at     ON opportunities(scanned_at);
        CREATE INDEX IF NOT EXISTS idx_opp_kalshi_ticker  ON opportunities(kalshi_ticker);
        CREATE INDEX IF NOT EXISTS idx_opp_tier           ON opportunities(tier);
        CREATE INDEX IF NOT EXISTS idx_trades_traded_at   ON trades(traded_at);
        CREATE INDEX IF NOT EXISTS idx_trades_status      ON trades(status);
        CREATE INDEX IF NOT EXISTS idx_trades_ticker      ON trades(kalshi_ticker);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def log_opportunity(
    conn: sqlite3.Connection,
    opp: Opportunity,
    executed: bool = False,
) -> int:
    """Insert one opportunity row. Returns the new row id."""
    km = opp.pair.kalshi
    pm = opp.pair.poly

    strategy = "A" if opp.kalshi_side == "YES" else "B"

    k_depth = opp.kalshi_depth_shares
    p_depth = opp.poly_depth_shares

    # tradeable = max units we could fill given depth on both sides
    if k_depth is not None and p_depth is not None:
        tradeable: int | None = int(min(k_depth, p_depth))
    elif k_depth is not None:
        tradeable = int(k_depth)
    elif p_depth is not None:
        tradeable = int(p_depth)
    else:
        tradeable = None

    max_profit = round(tradeable * opp.spread_cents / 100.0, 4) if tradeable is not None else None

    poly_token_id = (
        pm.yes_token_id if opp.poly_side == "YES" else pm.no_token_id
    ) or pm.platform_id

    try:
        cur = conn.execute(
            """
            INSERT INTO opportunities (
                scanned_at, kalshi_ticker, poly_token_id,
                kalshi_title, poly_title,
                strategy, kalshi_side, poly_side,
                kalshi_cost_cents, poly_cost_cents, spread_cents, tier,
                kalshi_depth_contracts, poly_depth_shares,
                tradeable_units, max_locked_profit_usd,
                hours_to_close, kalshi_close_time, poly_close_time,
                executed
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                opp.detected_at.isoformat(),
                km.platform_id,
                poly_token_id,
                km.raw_question,
                pm.raw_question,
                strategy,
                opp.kalshi_side,
                opp.poly_side,
                opp.kalshi_cost_cents,
                opp.poly_cost_cents,
                opp.spread_cents,
                opp.tier,
                k_depth,
                p_depth,
                tradeable,
                max_profit,
                opp.hours_to_close,
                km.resolution_dt.isoformat() if km.resolution_dt else None,
                pm.resolution_dt.isoformat() if pm.resolution_dt else None,
                1 if executed else 0,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        log.exception("DB | Failed to log opportunity for %s", km.platform_id)
        return -1


def mark_opportunity_executed(conn: sqlite3.Connection, opp_id: int) -> None:
    """Flip executed=1 on an opportunity after a trade is attempted."""
    if opp_id < 0:
        return
    try:
        conn.execute("UPDATE opportunities SET executed=1 WHERE id=?", (opp_id,))
        conn.commit()
    except Exception:
        log.exception("DB | Failed to mark opportunity %d as executed", opp_id)


def log_trade(
    conn: sqlite3.Connection,
    opp_id: int,
    opp: Opportunity,
    result,                             # ExecutionResult from arb_executor
    poly_balance_before: float | None = None,
) -> int:
    """Insert one trade row. Returns the new row id."""
    km = opp.pair.kalshi
    pm = opp.pair.poly

    poly_token_id = (
        pm.yes_token_id if opp.poly_side == "YES" else pm.no_token_id
    ) or pm.platform_id

    now = datetime.now(timezone.utc).isoformat()

    # For filled trades, requested == filled (arb_executor already adjusts for partial fills)
    kalshi_filled = result.units if result.status in ("filled", "unwound", "partial_stuck") else None
    poly_filled   = result.units if result.status == "filled" else None

    # Kalshi fee = 1.75% of face value (filled contracts × $1)
    kalshi_fee_usd = round(kalshi_filled * KALSHI_TAKER_FEE_RATE, 4) if kalshi_filled else None
    locked = result.guaranteed_profit_usd or None
    net_profit_usd = round(locked - (kalshi_fee_usd or 0.0), 4) if locked is not None else None

    try:
        cur = conn.execute(
            """
            INSERT INTO trades (
                opportunity_id, traded_at,
                kalshi_ticker, poly_token_id,
                kalshi_side, poly_side,
                requested_units, kalshi_filled, poly_filled,
                kalshi_price_cents, poly_price_cents,
                kalshi_cost_usd, poly_cost_usd, total_cost_usd,
                locked_profit_usd, kalshi_fee_usd, net_profit_usd,
                kalshi_order_id, poly_order_id,
                status, reason,
                kalshi_balance_before, poly_balance_before
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                opp_id if opp_id >= 0 else None,
                now,
                km.platform_id,
                poly_token_id,
                opp.kalshi_side,
                opp.poly_side,
                result.units or None,
                kalshi_filled,
                poly_filled,
                opp.kalshi_cost_cents,
                opp.poly_cost_cents,
                result.kalshi_cost_usd or None,
                result.poly_cost_usd or None,
                result.total_cost_usd or None,
                locked,
                kalshi_fee_usd,
                net_profit_usd,
                result.kalshi_order_id or None,
                result.poly_order_id or None,
                result.status,
                result.reason or None,
                None,                           # kalshi_balance_before (not exposed by executor)
                poly_balance_before,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        log.exception("DB | Failed to log trade for %s", km.platform_id)
        return -1

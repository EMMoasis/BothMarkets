"""
BothMarkets scanner — two-speed loop.

Market list refresh: every 2 hours (slow)
  → Fetches all Kalshi + Polymarket markets closing in 72h
  → Runs strict 4-criteria matching to build matched_pairs list

Price poll: every 2 seconds (fast)
  → Fetches live prices for all matched pairs
  → Evaluates Strategy A and B for each pair
  → Logs all pairs (with URLs) + any arbitrage opportunities found
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from scanner.config import (
    DB_FILE,
    DRY_RUN_DB_FILE,
    ENV_KALSHI_API_KEY,
    ENV_KALSHI_API_SECRET,
    ENV_POLY_API_KEY,
    ENV_POLY_API_PASSPHRASE,
    ENV_POLY_API_SECRET,
    ENV_POLY_FUNDER,
    ENV_POLY_PRIVATE_KEY,
    EXEC_MAX_TRADE_USD,
    FETCH_WORKERS,
    LOG_FILE,
    MARKET_REFRESH_SECONDS,
    OPPS_JSON_FILE,
    OPPS_LOG_FILE,
    PRICE_POLL_SECONDS,
)
from scanner.db import init_db, log_opportunity, log_trade, mark_opportunity_executed
from scanner.kalshi_client import KalshiClient
from scanner.market_matcher import MarketMatcher
from scanner.models import MatchedPair
from scanner.opportunity_finder import OpportunityFinder, format_opportunity_log
from scanner.poly_client import PolyClient

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Log filter — only pass opportunity/match lines to opportunities.log
# ------------------------------------------------------------------

class _OppsFilter(logging.Filter):
    _KEYWORDS = (
        "MATCH |", "PAIR  |", "ARB OPPORTUNITY",
        "SCAN CYCLE", "=== MARKET REFRESH",
        "EXEC |", "EXEC FILLED", "EXEC SKIP",
        "Kalshi order:", "Poly order:", "Kalshi unwind",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return any(kw in msg for kw in self._KEYWORDS)


# ------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------

def _load_env() -> None:
    """Load .env file from project root if present."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if v:
                    os.environ.setdefault(k, v)


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    main_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    main_handler.setFormatter(fmt)

    opps_handler = logging.FileHandler(OPPS_LOG_FILE, mode="a", encoding="utf-8")
    opps_handler.setFormatter(fmt)
    opps_handler.addFilter(_OppsFilter())

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[main_handler, opps_handler, console_handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)


def _save_opportunities_json(opportunities, run_ts: datetime) -> None:
    """Append this price cycle's opportunities to the NDJSON output file."""
    if not opportunities:
        return
    run_data = {
        "scan_timestamp": run_ts.isoformat(),
        "opportunity_count": len(opportunities),
        "opportunities": [
            {
                "tier": opp.tier,
                "kalshi_side": opp.kalshi_side,
                "poly_side": opp.poly_side,
                "kalshi_cost_cents": opp.kalshi_cost_cents,
                "poly_cost_cents": opp.poly_cost_cents,
                "combined_cost_cents": opp.combined_cost_cents,
                "spread_cents": opp.spread_cents,
                "hours_to_close": opp.hours_to_close,
                "kalshi_depth_shares": (
                    opp.pair.kalshi.yes_ask_depth if opp.kalshi_side == "YES"
                    else opp.pair.kalshi.no_ask_depth
                ),
                "poly_depth_shares": (
                    opp.pair.poly.yes_ask_depth if opp.poly_side == "YES"
                    else opp.pair.poly.no_ask_depth
                ),
                "asset": opp.pair.kalshi.asset,
                "direction": opp.pair.kalshi.direction,
                "threshold": opp.pair.kalshi.threshold,
                "kalshi_question": opp.pair.kalshi.raw_question,
                "kalshi_ticker": opp.pair.kalshi.platform_id,
                "kalshi_url": opp.pair.kalshi.platform_url,
                "poly_question": opp.pair.poly.raw_question,
                "poly_condition_id": opp.pair.poly.platform_id,
                "poly_url": opp.pair.poly.platform_url,
            }
            for opp in opportunities
        ],
    }
    with open(OPPS_JSON_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(run_data) + "\n")


# ------------------------------------------------------------------
# Price fetching for matched pairs
# ------------------------------------------------------------------

def _fetch_all_prices(
    pairs: list[MatchedPair],
    kalshi: KalshiClient,
    poly: PolyClient,
) -> tuple[dict, dict]:
    """
    Fetch live prices for all matched pairs from both platforms in parallel.

    Returns:
      kalshi_prices: {ticker: {yes_ask, no_ask, yes_bid, no_bid}}
      poly_prices:   {condition_id: {yes_ask, no_ask, yes_bid, no_bid}}
    """
    kalshi_markets = [p.kalshi for p in pairs]
    poly_markets = [p.poly for p in pairs]

    # Run both platform fetches in parallel
    kalshi_prices: dict = {}
    poly_prices: dict = {}

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_kalshi = pool.submit(kalshi.fetch_live_prices, kalshi_markets)
        f_poly = pool.submit(poly.fetch_clob_prices, poly_markets)
        kalshi_prices = f_kalshi.result()
        poly_prices = f_poly.result()

    return kalshi_prices, poly_prices


def _update_pair_prices(
    pairs: list[MatchedPair],
    kalshi_prices: dict,
    poly_prices: dict,
) -> list[MatchedPair]:
    """
    Inject freshly fetched prices into MatchedPair market objects.
    Returns new MatchedPair list with updated price fields.
    """
    from dataclasses import replace
    from scanner.models import NormalizedMarket

    updated: list[MatchedPair] = []
    for pair in pairs:
        kp = kalshi_prices.get(pair.kalshi.platform_id, {})
        pp = poly_prices.get(pair.poly.platform_id, {})

        km_updated = NormalizedMarket(
            **{**pair.kalshi.__dict__,
               "yes_ask_cents":  kp.get("yes_ask",       pair.kalshi.yes_ask_cents),
               "no_ask_cents":   kp.get("no_ask",        pair.kalshi.no_ask_cents),
               "yes_bid_cents":  kp.get("yes_bid",       pair.kalshi.yes_bid_cents),
               "no_bid_cents":   kp.get("no_bid",        pair.kalshi.no_bid_cents),
               "yes_ask_depth":  kp.get("yes_ask_depth", pair.kalshi.yes_ask_depth),
               "no_ask_depth":   kp.get("no_ask_depth",  pair.kalshi.no_ask_depth),
               }
        )
        pm_updated = NormalizedMarket(
            **{**pair.poly.__dict__,
               "yes_ask_cents":  pp.get("yes_ask",       pair.poly.yes_ask_cents),
               "no_ask_cents":   pp.get("no_ask",        pair.poly.no_ask_cents),
               "yes_bid_cents":  pp.get("yes_bid",       pair.poly.yes_bid_cents),
               "no_bid_cents":   pp.get("no_bid",        pair.poly.no_bid_cents),
               "yes_ask_depth":  pp.get("yes_ask_depth", pair.poly.yes_ask_depth),
               "no_ask_depth":   pp.get("no_ask_depth",  pair.poly.no_ask_depth),
               }
        )
        updated.append(MatchedPair(kalshi=km_updated, poly=pm_updated))

    return updated


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

def _is_paper_mode() -> bool:
    """Return True if the --paper flag was passed on the command line."""
    return "--paper" in sys.argv


def _init_executor(paper: bool = False):
    """
    Initialise the arbitrage executor.

    paper=True  → returns PaperArbExecutor (no real orders, $10K virtual wallet)
    paper=False → returns real ArbExecutor if credentials are present, else None
    """
    if paper:
        from scanner.paper_executor import PAPER_CAPITAL_USD, PaperArbExecutor
        log.info("*" * 60)
        log.info("  PAPER TRADING MODE — NO REAL ORDERS WILL BE PLACED")
        log.info("  Virtual capital: $%.2f", PAPER_CAPITAL_USD)
        log.info("*" * 60)
        return PaperArbExecutor()

    from scanner.arb_executor import ArbExecutor
    from scanner.kalshi_trader import KalshiTrader
    from scanner.poly_trader import PolyTrader

    k_key    = os.environ.get(ENV_KALSHI_API_KEY)
    k_secret = os.environ.get(ENV_KALSHI_API_SECRET)
    p_key    = os.environ.get(ENV_POLY_API_KEY)
    p_secret = os.environ.get(ENV_POLY_API_SECRET)
    p_pass   = os.environ.get(ENV_POLY_API_PASSPHRASE)
    p_priv   = os.environ.get(ENV_POLY_PRIVATE_KEY)
    p_funder = os.environ.get(ENV_POLY_FUNDER)

    if not all([k_key, k_secret, p_key, p_secret, p_pass, p_priv]):
        log.info("Trading DISABLED — one or more credentials missing (scan-only mode)")
        return None

    try:
        k_trader = KalshiTrader(api_key=k_key, api_secret_pem=k_secret)
        p_trader = PolyTrader(
            private_key=p_priv,
            api_key=p_key,
            api_secret=p_secret,
            api_passphrase=p_pass,
            funder=p_funder,
        )
        executor = ArbExecutor(
            kalshi=k_trader,
            poly=p_trader,
            max_trade_usd=EXEC_MAX_TRADE_USD,
        )
        log.info(
            "Trading ENABLED — max $%.2f per trade, kalshi_key=%s...",
            EXEC_MAX_TRADE_USD, k_key[:8],
        )
        return executor
    except Exception:
        log.exception("Failed to initialize executor — running in scan-only mode")
        return None


def main() -> None:
    _load_env()
    _setup_logging()

    paper = _is_paper_mode()
    db_file = DRY_RUN_DB_FILE if paper else DB_FILE

    log.info("=" * 60)
    log.info("BothMarkets scanner starting%s", " [PAPER MODE]" if paper else "")
    log.info("Market refresh every %d minutes, price poll every %ds",
             MARKET_REFRESH_SECONDS // 60, PRICE_POLL_SECONDS)

    kalshi = KalshiClient()
    poly = PolyClient()
    matcher = MarketMatcher()
    finder = OpportunityFinder()
    executor = _init_executor(paper=paper)
    db = init_db(db_file)

    matched_pairs: list[MatchedPair] = []
    last_market_refresh: float = 0.0
    price_cycle = 0
    total_opportunities = 0
    total_trades = 0

    try:
        while True:
            now_mono = time.monotonic()

            # --- Slow path: refresh market list every 2 hours ---
            if now_mono - last_market_refresh >= MARKET_REFRESH_SECONDS:
                log.info("=== MARKET REFRESH starting ===")
                try:
                    kalshi_markets = kalshi.get_all_markets(force_refresh=True)
                    poly_markets = poly.get_all_markets(force_refresh=True)
                    matched_pairs = matcher.find_matches(kalshi_markets, poly_markets)
                    last_market_refresh = time.monotonic()

                    log.info(
                        "=== MARKET REFRESH complete | K:%d P:%d markets | %d matched pairs ===",
                        len(kalshi_markets), len(poly_markets), len(matched_pairs),
                    )

                    if not matched_pairs:
                        log.info("No matched pairs found — verify parsing covers current market types")

                except Exception:
                    log.exception("Market refresh failed")
                    # Back off 30 s before retrying (avoids hammering APIs on 429s)
                    last_market_refresh = time.monotonic() - MARKET_REFRESH_SECONDS + 30

            # --- Fast path: fetch live prices and check for arb every 2 seconds ---
            if matched_pairs:
                price_cycle += 1
                if executor is not None:
                    executor.tick()
                cycle_start = time.monotonic()

                try:
                    kalshi_prices, poly_prices = _fetch_all_prices(matched_pairs, kalshi, poly)
                    live_pairs = _update_pair_prices(matched_pairs, kalshi_prices, poly_prices)

                    opportunities = finder.find_opportunities(live_pairs)
                    total_opportunities += len(opportunities)

                    # Log each pair with current prices (every cycle for test visibility)
                    for pair in live_pairs:
                        finder.log_pair_prices(pair)

                    # Log, execute, and persist opportunities
                    if opportunities:
                        scan_ts = datetime.now(timezone.utc)
                        for opp in opportunities:
                            log.info("ARB OPPORTUNITY | %s", format_opportunity_log(opp))

                            # Record every opportunity in the DB (executed flag updated below)
                            opp_id = log_opportunity(db, opp, executed=False)

                            # Execute if trading is enabled and pair not on cooldown
                            if executor is not None:
                                if executor.is_on_cooldown(opp):
                                    log.info(
                                        "EXEC SKIP (cooldown) | %s",
                                        opp.pair.kalshi.platform_id,
                                    )
                                    continue
                                try:
                                    result = executor.execute(opp)
                                    total_trades += 1
                                    log.info(
                                        "EXEC RESULT #%d | status=%s reason=%s "
                                        "units=%d cost=$%.4f profit=$%.4f | "
                                        "K=%s P=%s",
                                        total_trades, result.status, result.reason,
                                        result.units, result.total_cost_usd,
                                        result.guaranteed_profit_usd,
                                        result.kalshi_order_id or "N/A",
                                        result.poly_order_id or "N/A",
                                    )
                                    # Persist trade and mark opportunity as executed
                                    mark_opportunity_executed(db, opp_id)
                                    log_trade(db, opp_id, opp, result)
                                except Exception:
                                    log.exception(
                                        "Executor raised for %s",
                                        opp.pair.kalshi.platform_id,
                                    )

                        _save_opportunities_json(opportunities, scan_ts)

                    elapsed = round(time.monotonic() - cycle_start, 3)
                    log.info(
                        "SCAN CYCLE #%d | %.3fs | %d pairs | %d arb opportunities | "
                        "%d lifetime | %d trades",
                        price_cycle, elapsed, len(live_pairs), len(opportunities),
                        total_opportunities, total_trades,
                    )

                    # Paper mode: print wallet report every 100 cycles (~3 min)
                    if paper and executor is not None and price_cycle % 100 == 0:
                        log.info(executor.report())

                except Exception:
                    log.exception("Price cycle %d failed", price_cycle)

            # Sleep until next price poll
            cycle_elapsed = time.monotonic() - now_mono
            sleep_time = max(0.0, PRICE_POLL_SECONDS - cycle_elapsed)
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        log.info("Scanner stopped by user.")
        if paper and executor is not None:
            log.info(executor.report())


if __name__ == "__main__":
    main()

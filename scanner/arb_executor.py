"""Cross-platform arbitrage executor.

Takes a confirmed Opportunity and executes both legs:
  - Leg 1 (Kalshi):     BUY yes/no contracts at current ask
  - Leg 2 (Polymarket): BUY yes/no shares   at current ask

Strategy A: Buy Kalshi YES + Buy Polymarket NO
Strategy B: Buy Kalshi NO  + Buy Polymarket YES

Position sizing:
  - max_trade_usd caps total spend across both legs
  - Minimum: enough shares on Polymarket to meet its $1/leg minimum
  - Capped by available depth at best ask on both sides

Failure handling:
  If Leg 1 fills but Leg 2 fails:
    → Attempt to SELL the Kalshi contracts back at current bid
    → If unwind succeeds:    status = "unwound"
    → If unwind fails:       status = "partial_stuck"  (manual intervention needed)
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from scanner.config import (
    EXEC_COOLDOWN_CYCLES,
    EXEC_MAX_TRADE_USD,
    EXEC_POLY_MIN_ORDER_USD,
    EXEC_UNWIND_DELAY_SECONDS,
)
from scanner.kalshi_trader import KalshiTrader
from scanner.models import Opportunity
from scanner.poly_trader import PolyTrader

log = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of a two-leg arbitrage execution attempt."""
    status: str             # "filled" | "skipped" | "unwound" | "partial_stuck" | "error"
    reason: str = ""        # Short code for why status is non-"filled"

    units: int = 0          # Contracts/shares traded on each leg

    kalshi_order_id: str = ""
    poly_order_id: str = ""

    kalshi_cost_usd: float = 0.0
    poly_cost_usd: float = 0.0
    total_cost_usd: float = 0.0

    # On success: guaranteed profit locked in
    spread_cents: float = 0.0
    guaranteed_profit_usd: float = 0.0

    # On unwind: how much was recovered from selling Kalshi leg back
    unwind_recovered_usd: float = 0.0


class ArbExecutor:
    """
    Executes two-leg cross-platform arbitrage trades.

    Instantiate with authenticated KalshiTrader and PolyTrader.
    Call execute(opportunity) for each confirmed arbitrage opportunity.

    Tracks cooldowns per (kalshi_ticker, poly_id) pair to prevent
    hammering the same market every 2-second cycle.
    """

    def __init__(
        self,
        kalshi: KalshiTrader,
        poly: PolyTrader,
        max_trade_usd: float = EXEC_MAX_TRADE_USD,
    ) -> None:
        self._kalshi = kalshi
        self._poly = poly
        self._max_trade_usd = max_trade_usd
        # {(kalshi_ticker, poly_platform_id): cycle_number_until_ready}
        self._cooldowns: dict[tuple[str, str], int] = {}
        self._cycle = 0

    def tick(self) -> None:
        """Advance the internal cycle counter. Call once per price poll cycle."""
        self._cycle += 1

    def is_on_cooldown(self, opportunity: Opportunity) -> bool:
        """Return True if this pair was recently traded and should be skipped."""
        key = (opportunity.pair.kalshi.platform_id, opportunity.pair.poly.platform_id)
        return self._cycle < self._cooldowns.get(key, 0)

    def execute(self, opportunity: Opportunity) -> ExecutionResult:
        """
        Execute both legs of a cross-platform arbitrage trade.

        Validates the opportunity, sizes the position, places Kalshi first,
        then Polymarket. Handles Polymarket failure by unwinding Kalshi.

        Returns an ExecutionResult with full execution details.
        """
        opp = opportunity
        km = opp.pair.kalshi
        pm = opp.pair.poly
        k_side = opp.kalshi_side.lower()   # "yes" or "no"
        p_side = opp.poly_side             # "YES" or "NO"

        # Prices at time of opportunity detection (in cents)
        k_price_cents = opp.kalshi_cost_cents
        p_price_cents = opp.poly_cost_cents

        # Available depth
        k_depth = km.yes_ask_depth if k_side == "yes" else km.no_ask_depth
        p_depth = pm.yes_ask_depth if p_side == "YES" else pm.no_ask_depth

        # Select Polymarket token
        poly_token_id = pm.yes_token_id if p_side == "YES" else pm.no_token_id
        if not poly_token_id:
            return ExecutionResult(status="error", reason="missing_poly_token_id")

        # Guard: Polymarket wallet must have enough USDC to cover at least one leg
        try:
            poly_bal = self._poly.get_usdc_balance()
        except Exception as exc:
            log.warning("EXEC | Could not fetch Poly balance: %s — skipping", exc)
            return ExecutionResult(status="skipped", reason="poly_balance_check_failed")

        if poly_bal < EXEC_POLY_MIN_ORDER_USD:
            log.warning(
                "EXEC SKIP | Poly wallet balance $%.2f < min $%.2f — deposit USDC to wallet",
                poly_bal, EXEC_POLY_MIN_ORDER_USD,
            )
            return ExecutionResult(status="skipped", reason="poly_insufficient_balance")

        # Position sizing
        units = _calc_units(
            k_price_cents, p_price_cents,
            k_depth, p_depth,
            self._max_trade_usd,
        )
        if units < 1:
            log.info(
                "EXEC SKIP | %s | units=0 (k=%.1fc p=%.1fc max=$%.0f depth: k=%s p=%s)",
                km.platform_id, k_price_cents, p_price_cents,
                self._max_trade_usd,
                f"{k_depth:.0f}" if k_depth else "?",
                f"{p_depth:.0f}" if p_depth else "?",
            )
            return ExecutionResult(status="skipped", reason="insufficient_units")

        log.info(
            "EXEC | %s | Strategy %s | K-%s @ %dc  P-%s @ %dc | %d units | spread=%.2fc",
            km.platform_id,
            "A" if k_side == "yes" else "B",
            k_side.upper(), int(k_price_cents),
            p_side, int(p_price_cents),
            units, opp.spread_cents,
        )

        # ---- Leg 1: Kalshi ----
        k_price_int = int(round(k_price_cents))
        try:
            k_resp = self._kalshi.place_order(
                ticker=km.platform_id,
                side=k_side,
                count=units,
                price_cents=k_price_int,
                action="buy",
            )
            k_order_id = (k_resp.get("order") or {}).get("order_id", "")
            if not k_order_id:
                log.warning("EXEC | Kalshi order response missing order_id: %s", k_resp)
                return ExecutionResult(status="error", reason="kalshi_no_order_id")
        except Exception as exc:
            log.warning("EXEC | Kalshi leg failed: %s", exc)
            return ExecutionResult(status="skipped", reason="kalshi_leg_failed")

        # Small pause then verify actual Kalshi fill (partial fills leave remainder resting)
        time.sleep(0.5)
        try:
            k_order_info = self._kalshi.get_order(k_order_id)
            order_data = k_order_info.get("order", {})
            remaining = int(order_data.get("remaining_count") or 0)
            filled = units - remaining
            if remaining > 0:
                # Cancel the unfilled resting portion so it doesn't fill later unhedged
                try:
                    self._kalshi.cancel_order(k_order_id)
                    log.info("EXEC | Kalshi partial fill %d/%d — cancelled resting %d", filled, units, remaining)
                except Exception as ce:
                    log.warning("EXEC | Could not cancel resting Kalshi remainder: %s", ce)
            if filled < 1:
                log.warning("EXEC | Kalshi order placed but 0 contracts filled — aborting")
                return ExecutionResult(status="skipped", reason="kalshi_no_fill")
            if filled != units:
                log.info("EXEC | Adjusting Poly size from %d to %d (actual Kalshi fill)", units, filled)
                units = filled
        except Exception as exc:
            log.warning("EXEC | Could not verify Kalshi fill count (%s) — using requested %d units", exc, units)

        # ---- Leg 2: Polymarket ----
        p_price_frac = p_price_cents / 100.0
        try:
            p_resp = self._poly.place_order(
                token_id=poly_token_id,
                price=p_price_frac,
                size=float(units),
                side="BUY",
            )
            p_order_id = p_resp.get("orderID") or p_resp.get("id") or ""
        except Exception as exc:
            log.warning("EXEC | Polymarket leg FAILED after Kalshi filled: %s", exc)
            # Try to unwind Kalshi leg
            unwind_result = self._unwind_kalshi(
                ticker=km.platform_id,
                side=k_side,
                count=units,
                bought_price_cents=k_price_int,
            )
            self._set_cooldown(opp, EXEC_COOLDOWN_CYCLES * 2)
            return ExecutionResult(
                status="unwound" if unwind_result["ok"] else "partial_stuck",
                reason="poly_leg_failed",
                units=units,
                kalshi_order_id=k_order_id,
                kalshi_cost_usd=round(units * k_price_int / 100.0, 4),
                unwind_recovered_usd=round(unwind_result.get("recovered_usd", 0.0), 4),
            )

        # Both legs filled
        k_cost = round(units * k_price_int / 100.0, 4)
        p_cost = round(units * p_price_cents / 100.0, 4)
        total_cost = round(k_cost + p_cost, 4)
        guaranteed_profit = round(units * opp.spread_cents / 100.0, 4)

        log.info(
            "EXEC FILLED | %s | %d units | K=$%.4f P=$%.4f total=$%.4f | profit=$%.4f (spread=%.2fc)",
            km.platform_id, units, k_cost, p_cost, total_cost, guaranteed_profit, opp.spread_cents,
        )

        self._set_cooldown(opp, EXEC_COOLDOWN_CYCLES)
        return ExecutionResult(
            status="filled",
            units=units,
            kalshi_order_id=k_order_id,
            poly_order_id=p_order_id,
            kalshi_cost_usd=k_cost,
            poly_cost_usd=p_cost,
            total_cost_usd=total_cost,
            spread_cents=opp.spread_cents,
            guaranteed_profit_usd=guaranteed_profit,
        )

    # ------------------------------------------------------------------
    # Cooldown management
    # ------------------------------------------------------------------

    def _set_cooldown(self, opp: Opportunity, cycles: int) -> None:
        key = (opp.pair.kalshi.platform_id, opp.pair.poly.platform_id)
        self._cooldowns[key] = self._cycle + cycles

    # ------------------------------------------------------------------
    # Kalshi unwind (sell back Leg 1 if Leg 2 fails)
    # ------------------------------------------------------------------

    def _unwind_kalshi(
        self,
        ticker: str,
        side: str,
        count: int,
        bought_price_cents: int,
    ) -> dict[str, Any]:
        """
        Attempt to sell Kalshi contracts at current bid to recover capital.

        Tries up to 3 times with delays. Returns {"ok": True/False, "recovered_usd": float}.
        """
        MAX_RETRIES = 3
        RETRY_DELAY = 3.0

        for attempt in range(1, MAX_RETRIES + 1):
            time.sleep(EXEC_UNWIND_DELAY_SECONDS if attempt == 1 else RETRY_DELAY)
            try:
                prices = self._kalshi.get_market_price(ticker)
                bid_key = "yes_bid" if side == "yes" else "no_bid"
                current_bid = prices.get(bid_key)

                if current_bid is None:
                    log.warning(
                        "Kalshi unwind attempt %d/%d: no bid for %s %s",
                        attempt, MAX_RETRIES, ticker, side,
                    )
                    continue

                sell_price = max(1, int(math.floor(current_bid)))
                self._kalshi.place_order(
                    ticker=ticker,
                    side=side,
                    count=count,
                    price_cents=sell_price,
                    action="sell",
                )
                recovered = round(count * sell_price / 100.0, 4)
                log.info(
                    "Kalshi unwind OK: SELL %s %s ×%d @ %dc — recovered $%.4f",
                    side.upper(), ticker, count, sell_price, recovered,
                )
                return {"ok": True, "recovered_usd": recovered}

            except Exception as exc:
                if attempt < MAX_RETRIES:
                    log.warning(
                        "Kalshi unwind attempt %d/%d failed (%s), retrying in %.0fs",
                        attempt, MAX_RETRIES, exc, RETRY_DELAY,
                    )
                else:
                    log.error(
                        "Kalshi unwind FAILED after %d attempts for %s ×%d — PARTIAL STUCK",
                        MAX_RETRIES, ticker, count,
                    )

        return {"ok": False, "recovered_usd": 0.0}


# ------------------------------------------------------------------
# Position sizing helper
# ------------------------------------------------------------------

def _calc_units(
    k_price_cents: float,
    p_price_cents: float,
    k_depth: float | None,
    p_depth: float | None,
    max_trade_usd: float,
) -> int:
    """
    Calculate the number of contracts/shares to trade.

    Constraints:
      1. Total combined cost ≤ max_trade_usd
      2. Available depth at best ask on both sides
      3. Polymarket minimum order size: EXEC_POLY_MIN_ORDER_USD per leg

    Returns 0 if the trade cannot be made.
    """
    if k_price_cents <= 0 or p_price_cents <= 0:
        return 0

    k_price_frac = k_price_cents / 100.0
    p_price_frac = p_price_cents / 100.0
    combined_frac = k_price_frac + p_price_frac

    # Units by dollar cap
    max_by_usd = int(max_trade_usd / combined_frac) if combined_frac > 0 else 0

    # Units by depth (conservative: cap at min of both sides)
    max_by_depth = max_by_usd
    if k_depth is not None:
        max_by_depth = min(max_by_depth, int(k_depth))
    if p_depth is not None:
        max_by_depth = min(max_by_depth, int(p_depth))

    units = max_by_depth

    # Minimum: enough units so Polymarket leg meets $1 minimum
    if p_price_frac > 0:
        min_for_poly = math.ceil(EXEC_POLY_MIN_ORDER_USD / p_price_frac)
    else:
        min_for_poly = 1

    if units < min_for_poly:
        log.debug(
            "_calc_units: units=%d below poly minimum=%d — skipping",
            units, min_for_poly,
        )
        return 0

    return units

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
    EXEC_KALSHI_NO_FILL_COOLDOWN_CYCLES,
    EXEC_MAX_TRADE_USD,
    EXEC_MAX_UNITS_PER_MAP,
    EXEC_MAX_UNITS_PER_MARKET,
    EXEC_POLY_MIN_ORDER_USD,
    EXEC_UNWIND_DELAY_SECONDS,
    MIN_SPREAD_CENTS,
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

    kalshi_balance_before: float | None = None  # Kalshi cash balance before trade
    poly_balance_before: float | None = None    # Poly USDC balance before trade
    kalshi_balance_after: float | None = None   # Kalshi cash balance after trade
    poly_balance_after: float | None = None     # Poly USDC balance after trade


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
        # {kalshi_ticker: total_units_filled_this_session}
        # Tracks cumulative units on each market to enforce per-market cap.
        self._market_units: dict[str, int] = {}

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

        # Per-market cap check BEFORE balance fetch — no point hitting the API
        # if we've already maxed out this market for the session.
        # The _market_units counter never resets within a session, so once the cap
        # is hit this check will always reject.  A 30s cooldown keeps log noise low.
        market_units_so_far = self._market_units.get(km.platform_id, 0)
        if market_units_so_far >= EXEC_MAX_UNITS_PER_MARKET:
            log.info(
                "EXEC SKIP | %s | per-market cap reached (%d/%d units) — pausing this market",
                km.platform_id, market_units_so_far, EXEC_MAX_UNITS_PER_MARKET,
            )
            self._set_cooldown(opp, EXEC_KALSHI_NO_FILL_COOLDOWN_CYCLES)  # ~30s
            return ExecutionResult(status="skipped", reason="market_cap_reached")

        # Fetch both balances before trade for guard check and reconciliation
        try:
            poly_bal = self._poly.get_usdc_balance()
        except Exception as exc:
            log.warning("EXEC | Could not fetch Poly balance: %s — skipping", exc)
            return ExecutionResult(status="skipped", reason="poly_balance_check_failed")

        try:
            k_bal = self._kalshi.get_balance()
        except Exception as exc:
            log.warning("EXEC | Could not fetch Kalshi balance: %s — continuing", exc)
            k_bal = None

        log.info("EXEC | Balances before: Kalshi=$%.2f  Poly=$%.2f",
                 k_bal if k_bal is not None else -1, poly_bal)

        if poly_bal < EXEC_POLY_MIN_ORDER_USD:
            log.warning(
                "EXEC SKIP | Poly wallet balance $%.2f < min $%.2f — deposit USDC to wallet",
                poly_bal, EXEC_POLY_MIN_ORDER_USD,
            )
            return ExecutionResult(status="skipped", reason="poly_insufficient_balance",
                                   kalshi_balance_before=k_bal,
                                   poly_balance_before=poly_bal)

        # Position sizing — may walk the book and return a blended poly price
        units, effective_p_price = _calc_units(
            k_price_cents, p_price_cents,
            k_depth, p_depth,
            self._max_trade_usd,
            poly_ask_levels=opp.poly_ask_levels or [],
        )
        p_price_cents = effective_p_price  # may be blended if book was walked
        if units < 1:
            log.info(
                "EXEC SKIP | %s | units=0 (k=%.1fc p=%.1fc max=$%.0f depth: k=%s p=%s)",
                km.platform_id, k_price_cents, p_price_cents,
                self._max_trade_usd,
                f"{k_depth:.0f}" if k_depth else "?",
                f"{p_depth:.0f}" if p_depth else "?",
            )
            return ExecutionResult(status="skipped", reason="insufficient_units",
                                   kalshi_balance_before=k_bal,
                                   poly_balance_before=poly_bal)

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
            # 409 = position limit hit or market non-tradeable — cool down longer
            is_conflict = "409" in str(exc)
            log.warning("EXEC | Kalshi leg failed (%s): %s",
                        "409 conflict" if is_conflict else "error", exc)
            self._set_cooldown(opp, EXEC_COOLDOWN_CYCLES * 6 if is_conflict else 1)
            return ExecutionResult(status="skipped", reason="kalshi_conflict" if is_conflict else "kalshi_leg_failed")

        # Small pause then verify actual Kalshi fill.
        # IMPORTANT: remaining_count=0 does NOT mean filled — it also equals 0 for
        # cancelled orders.  Always check the explicit `fill_count` field and the
        # order `status`.  A status of "canceled" with fill_count=0 is a 0-fill.
        time.sleep(0.5)
        try:
            k_order_info = self._kalshi.get_order(k_order_id)
            order_data = k_order_info.get("order", {})
            order_status = (order_data.get("status") or "").lower()
            # fill_count is the authoritative field for how many contracts were filled
            filled = int(order_data.get("fill_count") or 0)
            remaining = int(order_data.get("remaining_count") or 0)

            if remaining > 0 and order_status not in ("canceled", "cancelled"):
                # Partial fill: cancel the resting portion so it doesn't fill later unhedged
                try:
                    self._kalshi.cancel_order(k_order_id)
                    log.info("EXEC | Kalshi partial fill %d/%d — cancelled resting %d", filled, units, remaining)
                except Exception as ce:
                    log.warning("EXEC | Could not cancel resting Kalshi remainder: %s", ce)

            log.info("EXEC | Kalshi order status=%s fill_count=%d remaining=%d", order_status, filled, remaining)

            if filled < 1:
                log.warning("EXEC | Kalshi order %s with 0 fills (status=%s) — aborting",
                            k_order_id[:16], order_status)
                self._set_cooldown(opp, EXEC_KALSHI_NO_FILL_COOLDOWN_CYCLES)
                log.info("EXEC SKIP (kalshi_no_fill) | %s — cooling down %ds",
                         km.platform_id, EXEC_KALSHI_NO_FILL_COOLDOWN_CYCLES * 2)
                return ExecutionResult(status="skipped", reason="kalshi_no_fill",
                                       kalshi_balance_before=k_bal,
                                       poly_balance_before=poly_bal)
            if filled != units:
                log.info("EXEC | Adjusting Poly size from %d to %d (actual Kalshi fill)", units, filled)
                units = filled
        except Exception as exc:
            log.warning("EXEC | Could not verify Kalshi fill count (%s) — using requested %d units", exc, units)

        # ---- Leg 2: Polymarket ----
        p_price_frac = p_price_cents / 100.0
        p_order_id = ""
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
            p_order_id = ""

        # ---- Verify actual Polymarket fill ----
        # FOK orders either fill immediately or are killed — but the fill amount
        # may differ from what we requested (Poly fills at most size×price budget).
        # CRITICAL: always check actual fill before declaring success.  If Poly
        # filled 0 shares we MUST unwind the Kalshi leg to avoid a naked position.
        poly_actual_fill: float = 0.0
        if p_order_id:
            time.sleep(0.5)  # give CLOB a moment to settle
            poly_actual_fill = self._poly.get_actual_fill(p_order_id, float(units))
            log.info("EXEC | Poly actual fill: %.0f / %d shares (order=%s...)",
                     poly_actual_fill, units, p_order_id[:16])
        else:
            log.warning("EXEC | Poly order returned no order_id — treating as 0 fill")

        if poly_actual_fill < 1.0:
            # Poly leg did not fill at all → unwind the Kalshi leg immediately
            log.warning(
                "EXEC | Polymarket 0-fill after Kalshi filled %d units — unwinding Kalshi",
                units,
            )
            unwind_result = self._unwind_kalshi(
                ticker=km.platform_id,
                side=k_side,
                count=units,
                bought_price_cents=k_price_int,
            )
            self._set_cooldown(opp, EXEC_COOLDOWN_CYCLES * 2)
            k_bal_after, poly_bal_after = self._reconcile_balances(
                label=km.platform_id,
                k_bal_before=k_bal,
                poly_bal_before=poly_bal,
                expected_k_delta=-round(units * k_price_int / 100.0, 4),
                expected_p_delta=0.0,
            )
            return ExecutionResult(
                status="unwound" if unwind_result["ok"] else "partial_stuck",
                reason="poly_0_fill",
                units=units,
                kalshi_order_id=k_order_id,
                poly_order_id=p_order_id,
                kalshi_cost_usd=round(units * k_price_int / 100.0, 4),
                unwind_recovered_usd=round(unwind_result.get("recovered_usd", 0.0), 4),
                kalshi_balance_before=k_bal,
                poly_balance_before=poly_bal,
                kalshi_balance_after=k_bal_after,
                poly_balance_after=poly_bal_after,
            )

        # Poly partially or fully filled — use actual fill count
        poly_units = int(poly_actual_fill)
        if poly_units < units:
            log.warning(
                "EXEC | Poly partial fill: %d / %d shares — position partially hedged",
                poly_units, units,
            )
            # Kalshi was over-filled vs Poly.  The unhedged Kalshi surplus is
            # (units - poly_units) contracts.  Record actual amounts; caller can
            # decide whether to unwind the surplus manually.
            units = poly_units  # align recorded units to the smaller (hedged) amount

        # Both legs filled (possibly with partial mismatch already handled)

        k_cost = round(units * k_price_int / 100.0, 4)
        p_cost = round(units * p_price_cents / 100.0, 4)
        total_cost = round(k_cost + p_cost, 4)
        guaranteed_profit = round(units * opp.spread_cents / 100.0, 4)

        log.info(
            "EXEC FILLED | %s | %d units | K=$%.4f P=$%.4f total=$%.4f | profit=$%.4f (spread=%.2fc)",
            km.platform_id, units, k_cost, p_cost, total_cost, guaranteed_profit, opp.spread_cents,
        )

        k_bal_after, poly_bal_after = self._reconcile_balances(
            label=km.platform_id,
            k_bal_before=k_bal,
            poly_bal_before=poly_bal,
            expected_k_delta=-k_cost,
            expected_p_delta=-p_cost,
        )

        # Update per-market unit counter
        self._market_units[km.platform_id] = self._market_units.get(km.platform_id, 0) + units
        log.info("EXEC | Market %s cumulative units this session: %d/%d",
                 km.platform_id, self._market_units[km.platform_id], EXEC_MAX_UNITS_PER_MARKET)

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
            kalshi_balance_before=k_bal,
            poly_balance_before=poly_bal,
            kalshi_balance_after=k_bal_after,
            poly_balance_after=poly_bal_after,
        )

    # ------------------------------------------------------------------
    # Post-trade balance reconciliation
    # ------------------------------------------------------------------

    _RECONCILE_GAP_THRESHOLD = 0.50  # Warn if actual delta differs from expected by more than $0.50

    def _reconcile_balances(
        self,
        label: str,
        k_bal_before: float | None,
        poly_bal_before: float | None,
        expected_k_delta: float,
        expected_p_delta: float,
    ) -> tuple[float | None, float | None]:
        """
        Fetch fresh balances from both platforms after a trade and compare
        them to what we expected based on the fill costs.

        Logs a WARNING if either platform's actual balance delta differs
        from the expected delta by more than _RECONCILE_GAP_THRESHOLD,
        which may indicate the trade did not settle as logged.

        Returns (k_bal_after, poly_bal_after) — either may be None on error.
        """
        try:
            k_bal_after: float | None = self._kalshi.get_balance()
        except Exception as exc:
            log.warning("RECONCILE | Could not fetch Kalshi balance after trade: %s", exc)
            k_bal_after = None

        try:
            poly_bal_after: float | None = self._poly.get_usdc_balance()
        except Exception as exc:
            log.warning("RECONCILE | Could not fetch Poly balance after trade: %s", exc)
            poly_bal_after = None

        log.info("RECONCILE | %s | Balances after: Kalshi=$%.2f  Poly=$%.2f",
                 label,
                 k_bal_after if k_bal_after is not None else -1,
                 poly_bal_after if poly_bal_after is not None else -1)

        # Kalshi reconciliation
        if k_bal_before is not None and k_bal_after is not None:
            actual_k_delta = k_bal_after - k_bal_before
            k_gap = abs(actual_k_delta - expected_k_delta)
            if k_gap > self._RECONCILE_GAP_THRESHOLD:
                log.warning(
                    "RECONCILE WARNING | %s | Kalshi balance gap $%.4f — "
                    "expected delta=$%.4f  actual delta=$%.4f  "
                    "(before=$%.2f  after=$%.2f) — verify trade settled correctly",
                    label, k_gap, expected_k_delta, actual_k_delta,
                    k_bal_before, k_bal_after,
                )
            else:
                log.info("RECONCILE OK | %s | Kalshi delta=$%.4f (expected=$%.4f)",
                         label, actual_k_delta, expected_k_delta)

        # Poly reconciliation
        if poly_bal_before is not None and poly_bal_after is not None:
            actual_p_delta = poly_bal_after - poly_bal_before
            p_gap = abs(actual_p_delta - expected_p_delta)
            if p_gap > self._RECONCILE_GAP_THRESHOLD:
                log.warning(
                    "RECONCILE WARNING | %s | Poly balance gap $%.4f — "
                    "expected delta=$%.4f  actual delta=$%.4f  "
                    "(before=$%.2f  after=$%.2f) — verify trade settled correctly",
                    label, p_gap, expected_p_delta, actual_p_delta,
                    poly_bal_before, poly_bal_after,
                )
            else:
                log.info("RECONCILE OK | %s | Poly delta=$%.4f (expected=$%.4f)",
                         label, actual_p_delta, expected_p_delta)

        return k_bal_after, poly_bal_after

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
    poly_ask_levels: list[tuple[float, float]] | None = None,
) -> tuple[int, float]:
    """
    Calculate the number of contracts/shares to trade and the effective Polymarket price.

    Constraints:
      1. Total combined cost ≤ max_trade_usd
      2. Available depth at best ask on both sides
      3. Polymarket minimum order size: EXEC_POLY_MIN_ORDER_USD per leg

    When best-ask depth on Polymarket is below the $1 minimum, the function walks
    the ask ladder (poly_ask_levels) to accumulate enough shares.  The spread is
    re-checked after blending; if it falls below MIN_SPREAD_CENTS the trade is
    still rejected.

    Returns (units, effective_poly_price_cents).
    Returns (0, 0.0) if the trade cannot be made.
    """
    if k_price_cents <= 0 or p_price_cents <= 0:
        return 0, 0.0

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

    # Hard cap per map market to avoid over-investment on thin books
    max_by_depth = min(max_by_depth, EXEC_MAX_UNITS_PER_MAP)

    units = max_by_depth

    # Minimum: enough units so Polymarket leg meets $1 minimum
    if p_price_frac > 0:
        min_for_poly = math.ceil(EXEC_POLY_MIN_ORDER_USD / p_price_frac)
    else:
        min_for_poly = 1

    if units >= min_for_poly:
        # Happy path: best-ask depth is sufficient
        return units, p_price_cents

    # --- Book-walk fallback ---
    # Best-ask depth is below the $1 minimum. Try to collect enough shares by
    # consuming additional ask levels, blending the price as we go.
    if not poly_ask_levels:
        log.debug(
            "_calc_units: units=%d below poly minimum=%d and no book levels — skipping",
            units, min_for_poly,
        )
        return 0, 0.0

    collected = 0
    total_poly_cost_cents = 0.0
    for level_price, level_size in poly_ask_levels:   # sorted ascending = best ask first
        if collected >= min_for_poly:
            break
        take = min(int(level_size), min_for_poly - collected)
        collected += take
        total_poly_cost_cents += take * level_price

    if collected < min_for_poly:
        log.debug(
            "_calc_units: walked full book (%d levels), only %d/%d units — skipping",
            len(poly_ask_levels), collected, min_for_poly,
        )
        return 0, 0.0

    blended_price = total_poly_cost_cents / collected

    # Re-check spread with the blended (higher) poly price
    new_spread = 100.0 - k_price_cents - blended_price
    if new_spread < MIN_SPREAD_CENTS:
        log.debug(
            "_calc_units: book-walk blended poly=%.2fc → spread=%.2fc < min %.2fc — skipping",
            blended_price, new_spread, MIN_SPREAD_CENTS,
        )
        return 0, 0.0

    # Re-cap by Kalshi depth and dollar cap at the new blended combined cost
    blended_combined_frac = (k_price_cents + blended_price) / 100.0
    max_by_usd_blended = int(max_trade_usd / blended_combined_frac) if blended_combined_frac > 0 else 0
    max_k = int(k_depth) if k_depth is not None else collected
    final_units = min(collected, max_k, max_by_usd_blended)

    if final_units < min_for_poly:
        return 0, 0.0

    log.info(
        "_calc_units: book-walk filled %d units at blended poly=%.2fc (spread=%.2fc)",
        final_units, blended_price, new_spread,
    )
    return final_units, blended_price

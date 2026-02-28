"""
Paper (dry-run) arbitrage executor.

Drop-in replacement for ArbExecutor — same public interface (tick, is_on_cooldown,
execute, report), but places NO real orders.  Simulates fills at the current ask
price (full fill assumed, no slippage) and tracks a virtual wallet.

Usage:
    Run the scanner with the --paper flag:
        py -m scanner.runner --paper

    Or via the "Paper Trade" launch configuration in .claude/launch.json.

The paper executor writes to scanner_paper.db (separate from the live scanner.db)
so simulated data never pollutes real trade history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from scanner.arb_executor import ExecutionResult, _calc_units
from scanner.config import (
    EXEC_COOLDOWN_CYCLES,
    EXEC_MAX_TRADE_USD,
    EXEC_POLY_MIN_ORDER_USD,
    KALSHI_TAKER_FEE_RATE,
)
from scanner.models import Opportunity

log = logging.getLogger(__name__)

# Default paper capital and split (can be overridden at construction time)
PAPER_CAPITAL_USD: float = 20_000.0   # $10K Kalshi + $10K Polymarket
PAPER_KALSHI_RATIO: float = 0.5   # 50 % Kalshi, 50 % Polymarket


# ---------------------------------------------------------------------------
# Internal wallet state
# ---------------------------------------------------------------------------

@dataclass
class _PaperWallet:
    kalshi_balance: float
    poly_balance: float

    total_invested: float = 0.0
    total_gross_profit: float = 0.0
    total_kalshi_fees: float = 0.0
    trade_count: int = 0

    best_profit: float = 0.0
    best_trade_label: str = ""
    worst_profit: float = float("inf")
    worst_trade_label: str = ""

    @property
    def net_profit(self) -> float:
        return round(self.total_gross_profit - self.total_kalshi_fees, 4)

    @property
    def deployed_roi_pct(self) -> float:
        if self.total_invested == 0:
            return 0.0
        return round(self.net_profit / self.total_invested * 100, 2)


# ---------------------------------------------------------------------------
# Paper executor
# ---------------------------------------------------------------------------

class PaperArbExecutor:
    """
    Simulates cross-platform arbitrage trades against a virtual wallet.

    Assumptions:
      - Full fill at current ask price (no partial fills, no slippage)
      - Kalshi taker fee = KALSHI_TAKER_FEE_RATE × filled contracts
      - Both legs always succeed (no Polymarket failure scenarios in paper mode)
      - Cooldown logic mirrors the real executor
    """

    def __init__(
        self,
        total_capital: float = PAPER_CAPITAL_USD,
        max_trade_usd: float = EXEC_MAX_TRADE_USD,
        kalshi_ratio: float = PAPER_KALSHI_RATIO,
    ) -> None:
        k_bal = round(total_capital * kalshi_ratio, 2)
        p_bal = round(total_capital - k_bal, 2)
        self._wallet = _PaperWallet(kalshi_balance=k_bal, poly_balance=p_bal)
        self._max_trade_usd = max_trade_usd
        self._cooldowns: dict[tuple[str, str], int] = {}
        self._cycle = 0
        self._initial_capital = total_capital

        log.info(
            "PAPER MODE | Virtual wallet initialised: Kalshi=$%.2f | Poly=$%.2f | Total=$%.2f",
            k_bal, p_bal, total_capital,
        )

    # ------------------------------------------------------------------
    # Public interface (mirrors ArbExecutor)
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Advance the internal cycle counter. Call once per price poll cycle."""
        self._cycle += 1

    def is_on_cooldown(self, opportunity: Opportunity) -> bool:
        key = _pair_key(opportunity)
        return self._cycle < self._cooldowns.get(key, 0)

    def execute(self, opportunity: Opportunity) -> ExecutionResult:
        """
        Simulate execution of a two-leg arb trade.

        Returns an ExecutionResult identical in shape to the real executor,
        with order IDs prefixed 'PAPER-' so they're clearly distinguishable.
        """
        opp = opportunity
        km = opp.pair.kalshi
        pm = opp.pair.poly
        k_side = opp.kalshi_side.lower()
        p_side = opp.poly_side

        k_price_cents = opp.kalshi_cost_cents
        p_price_cents = opp.poly_cost_cents

        k_depth = km.yes_ask_depth if k_side == "yes" else km.no_ask_depth
        p_depth = pm.yes_ask_depth if p_side == "YES" else pm.no_ask_depth

        poly_token_id = pm.yes_token_id if p_side == "YES" else pm.no_token_id
        if not poly_token_id:
            return ExecutionResult(status="skipped", reason="missing_poly_token_id")

        # Virtual balance guard
        if self._wallet.poly_balance < EXEC_POLY_MIN_ORDER_USD:
            log.warning(
                "PAPER SKIP | Poly virtual balance $%.2f < min $%.2f",
                self._wallet.poly_balance, EXEC_POLY_MIN_ORDER_USD,
            )
            return ExecutionResult(status="skipped", reason="poly_insufficient_balance")

        # Position sizing — same rules as real executor.
        # _calc_units may walk the book and return a blended poly price.
        units, effective_p_price = _calc_units(
            k_price_cents, p_price_cents,
            k_depth, p_depth,
            self._max_trade_usd,
            poly_ask_levels=opp.poly_ask_levels or [],
        )

        # Additionally cap by virtual wallet balances (use effective poly price)
        if units > 0 and k_price_cents > 0 and effective_p_price > 0:
            max_by_k = int(self._wallet.kalshi_balance / (k_price_cents / 100.0))
            max_by_p = int(self._wallet.poly_balance   / (effective_p_price / 100.0))
            units = min(units, max_by_k, max_by_p)

        if units < 1:
            return ExecutionResult(status="skipped", reason="insufficient_units")

        # Use the effective (possibly blended) poly price for cost calculation
        p_price_cents = effective_p_price

        # --- Simulate fill ---
        k_cost          = round(units * k_price_cents / 100.0, 4)
        p_cost          = round(units * p_price_cents / 100.0, 4)
        total_cost      = round(k_cost + p_cost, 4)
        # Recalculate spread using effective price (may differ from opp.spread_cents
        # when we walked the book and blended a higher poly price)
        effective_spread = round(100.0 - k_price_cents - p_price_cents, 4)
        gross_profit    = round(units * effective_spread / 100.0, 4)
        kalshi_fee      = round(units * KALSHI_TAKER_FEE_RATE, 4)
        net_profit      = round(gross_profit - kalshi_fee, 4)

        # Deduct from virtual wallet
        self._wallet.kalshi_balance   = round(self._wallet.kalshi_balance - k_cost, 4)
        self._wallet.poly_balance     = round(self._wallet.poly_balance - p_cost, 4)
        self._wallet.total_invested   = round(self._wallet.total_invested + total_cost, 4)
        self._wallet.total_gross_profit = round(self._wallet.total_gross_profit + gross_profit, 4)
        self._wallet.total_kalshi_fees  = round(self._wallet.total_kalshi_fees + kalshi_fee, 4)
        self._wallet.trade_count += 1

        # Track best / worst
        trade_label = (
            f"{km.platform_id} | {opp.spread_cents:.1f}c spread | {units} units"
        )
        if gross_profit > self._wallet.best_profit:
            self._wallet.best_profit = gross_profit
            self._wallet.best_trade_label = trade_label
        if gross_profit < self._wallet.worst_profit:
            self._wallet.worst_profit = gross_profit
            self._wallet.worst_trade_label = trade_label

        self._set_cooldown(opp, EXEC_COOLDOWN_CYCLES)

        log.info(
            "PAPER FILLED #%d | %s | %d units | "
            "K=$%.4f P=$%.4f total=$%.4f | "
            "gross=$%.4f fee=$%.4f net=$%.4f | "
            "Wallet K=$%.2f P=$%.2f",
            self._wallet.trade_count,
            km.platform_id, units,
            k_cost, p_cost, total_cost,
            gross_profit, kalshi_fee, net_profit,
            self._wallet.kalshi_balance, self._wallet.poly_balance,
        )

        return ExecutionResult(
            status="filled",
            units=units,
            kalshi_order_id=f"PAPER-K-{self._wallet.trade_count:04d}",
            poly_order_id=f"PAPER-P-{self._wallet.trade_count:04d}",
            kalshi_cost_usd=k_cost,
            poly_cost_usd=p_cost,
            total_cost_usd=total_cost,
            spread_cents=opp.spread_cents,
            guaranteed_profit_usd=gross_profit,
        )

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def report(self) -> str:
        """Return a multi-line paper trading summary suitable for logging."""
        w = self._wallet
        remaining = round(w.kalshi_balance + w.poly_balance, 2)
        deployed_pct = round(w.total_invested / self._initial_capital * 100, 1) if self._initial_capital else 0.0

        lines = [
            "",
            "=" * 60,
            "  PAPER TRADING REPORT",
            "=" * 60,
            f"  Initial capital   : ${self._initial_capital:>10,.2f}",
            f"  Kalshi balance    : ${w.kalshi_balance:>10,.2f}",
            f"  Poly balance      : ${w.poly_balance:>10,.2f}",
            f"  Deployed          : ${w.total_invested:>10,.4f}  ({deployed_pct:.1f}% of capital)",
            "",
            f"  Trades simulated  : {w.trade_count}",
            f"  Gross profit      : ${w.total_gross_profit:>10.4f}",
            f"  Kalshi fees (est) : ${w.total_kalshi_fees:>10.4f}",
            f"  Net profit        : ${w.net_profit:>10.4f}",
            f"  Net ROI on deployed: {w.deployed_roi_pct:>8.2f}%",
        ]

        if w.trade_count > 0:
            worst = w.worst_profit if w.worst_profit < float("inf") else 0.0
            lines += [
                "",
                f"  Best trade  : ${w.best_profit:.4f}  — {w.best_trade_label}",
                f"  Worst trade : ${worst:.4f}  — {w.worst_trade_label}",
            ]

        lines.append("=" * 60)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_cooldown(self, opp: Opportunity, cycles: int) -> None:
        self._cooldowns[_pair_key(opp)] = self._cycle + cycles


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _pair_key(opp: Opportunity) -> tuple[str, str]:
    return (opp.pair.kalshi.platform_id, opp.pair.poly.platform_id)

"""Tests for scanner.arb_executor — two-leg arbitrage execution orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from scanner.arb_executor import ArbExecutor, ExecutionResult, _calc_units
from scanner.models import MatchedPair, NormalizedMarket, Opportunity, MarketType, Platform


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_nm(
    platform: Platform = Platform.KALSHI,
    platform_id: str = "KXTEST-001",
    yes_ask_cents: float | None = 55.0,
    no_ask_cents: float | None = 47.0,
    yes_ask_depth: float | None = 100.0,
    no_ask_depth: float | None = 100.0,
    yes_token_id: str | None = "yt-001",
    no_token_id: str | None = "nt-001",
) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        platform_id=platform_id,
        platform_url="https://example.com",
        raw_question="Test question",
        market_type=MarketType.SPORTS,
        sport="CS2",
        team="team_a",
        opponent="team_b",
        resolution_dt=datetime(2026, 3, 1, tzinfo=timezone.utc),
        yes_ask_cents=yes_ask_cents,
        no_ask_cents=no_ask_cents,
        yes_ask_depth=yes_ask_depth,
        no_ask_depth=no_ask_depth,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
    )


def _make_opportunity(
    kalshi_side: str = "YES",
    poly_side: str = "NO",
    k_cost: float = 55.0,
    p_cost: float = 40.0,
    k_depth: float | None = 100.0,
    p_depth: float | None = 100.0,
) -> Opportunity:
    km = _make_nm(
        platform=Platform.KALSHI,
        yes_ask_cents=k_cost if kalshi_side == "YES" else None,
        no_ask_cents=k_cost if kalshi_side == "NO" else None,
        yes_ask_depth=k_depth if kalshi_side == "YES" else None,
        no_ask_depth=k_depth if kalshi_side == "NO" else None,
    )
    pm = _make_nm(
        platform=Platform.POLYMARKET,
        platform_id="poly-001_team_a",
        yes_ask_cents=p_cost if poly_side == "YES" else None,
        no_ask_cents=p_cost if poly_side == "NO" else None,
        yes_ask_depth=p_depth if poly_side == "YES" else None,
        no_ask_depth=p_depth if poly_side == "NO" else None,
    )
    spread = 100.0 - (k_cost + p_cost)
    return Opportunity(
        pair=MatchedPair(kalshi=km, poly=pm),
        kalshi_side=kalshi_side,
        poly_side=poly_side,
        kalshi_cost_cents=k_cost,
        poly_cost_cents=p_cost,
        combined_cost_cents=k_cost + p_cost,
        spread_cents=spread,
        tier="Mid",
        hours_to_close=5.0,
        detected_at=datetime.now(timezone.utc),
        kalshi_depth_shares=k_depth,
        poly_depth_shares=p_depth,
    )


def _make_executor(max_trade_usd: float = 5.0) -> tuple[ArbExecutor, MagicMock, MagicMock]:
    k_trader = MagicMock()
    p_trader = MagicMock()
    # Default: enough balance to pass the guard (tests that need $0 override explicitly)
    p_trader.get_usdc_balance.return_value = 100.0
    # Default: Kalshi balance for reconciliation
    k_trader.get_balance.return_value = 500.0
    # Default: Kalshi order fully filled.
    # fill_count=5 matches the default 5-unit trade (k=55c p=40c max=$5).
    # Tests needing different fill counts override get_order explicitly.
    k_trader.get_order.return_value = {"order": {"status": "filled", "fill_count": 5, "remaining_count": 0}}
    # Default: Poly FOK order fully filled (get_actual_fill returns requested size)
    # Individual tests override this to simulate 0-fill or partial-fill scenarios.
    p_trader.get_actual_fill.side_effect = lambda order_id, estimated: estimated
    executor = ArbExecutor(kalshi=k_trader, poly=p_trader, max_trade_usd=max_trade_usd)
    return executor, k_trader, p_trader


# ---------------------------------------------------------------------------
# _calc_units
# ---------------------------------------------------------------------------

class TestCalcUnits:
    def test_basic_sizing(self):
        # k=55c p=40c combined=95c → $0.95/unit
        # max_trade_usd=5.0 → floor(5/0.95) = 5 units
        units, price = _calc_units(55.0, 40.0, 100.0, 100.0, 5.0)
        assert units == 5
        assert price == 40.0

    def test_capped_by_kalshi_depth(self):
        # max_by_usd=5, but k_depth=3 → 3 units
        units, price = _calc_units(55.0, 40.0, 3.0, 100.0, 5.0)
        assert units == 3
        assert price == 40.0

    def test_capped_by_poly_depth(self):
        # max_by_usd=5, p_depth=4, min_for_poly=ceil(1/0.40)=3 → 4 units
        units, price = _calc_units(55.0, 40.0, 100.0, 4.0, 5.0)
        assert units == 4
        assert price == 40.0

    def test_depth_none_uses_usd_cap(self):
        # No depth info → use only max_trade_usd
        units, price = _calc_units(55.0, 40.0, None, None, 5.0)
        assert units == 5
        assert price == 40.0

    def test_zero_price_returns_zero(self):
        assert _calc_units(0.0, 40.0, 100.0, 100.0, 5.0) == (0, 0.0)
        assert _calc_units(55.0, 0.0, 100.0, 100.0, 5.0) == (0, 0.0)

    def test_below_poly_minimum_no_levels_returns_zero(self):
        # p=40c per unit → min_for_poly = ceil(1.0/0.40) = 3
        # depth=2, no book levels → skip
        units, price = _calc_units(55.0, 40.0, 2.0, 2.0, 5.0)
        assert units == 0
        assert price == 0.0

    def test_large_depth_limited_by_usd(self):
        # k=30c, p=60c combined=90c, min_for_poly=ceil(1/0.60)=2
        # max_by_usd=floor(2.0/0.90)=2 >= min_for_poly=2 → 2 units
        units, price = _calc_units(30.0, 60.0, 1000.0, 1000.0, 2.0)
        assert units == 2
        assert price == 60.0

    def test_high_price_near_100c(self):
        # k=80c p=50c combined=130c (edge case near combined=100c)
        # max_by_usd=floor(5/1.30)=3, min_for_poly=ceil(1/0.50)=2 → 3 units
        units, price = _calc_units(80.0, 50.0, 100.0, 100.0, 5.0)
        assert units == 3
        assert price == 50.0

    # --- Book-walk tests ---

    def test_book_walk_meets_minimum(self):
        # k=72c, p=18c best-ask (5 shares) → min_for_poly=ceil(1/0.18)=6
        # depth=5, best ask only has 5 → need to walk
        # Next level: 20c with 10 shares → can collect 1 more from it
        # blended = (5*18 + 1*20)/6 = (90+20)/6 = 18.33c
        # new spread = 100 - 72 - 18.33 = 9.67c > MIN (3.3c) → valid
        levels = [(18.0, 5.0), (20.0, 10.0)]
        units, price = _calc_units(72.0, 18.0, 300.0, 5.0, 50.0, poly_ask_levels=levels)
        assert units == 6
        assert round(price, 4) == round((5*18 + 1*20) / 6, 4)

    def test_book_walk_spread_too_tight_after_blend(self):
        # k=72c, p=24c best-ask (1 share) → min_for_poly=ceil(1/0.24)=5
        # Next levels very expensive: 70c → blended ≈ 65c+ → spread goes negative
        levels = [(24.0, 1.0), (70.0, 100.0)]
        units, price = _calc_units(72.0, 24.0, 300.0, 1.0, 50.0, poly_ask_levels=levels)
        assert units == 0
        assert price == 0.0

    def test_book_walk_no_levels_returns_zero(self):
        # Depth too thin and no levels provided
        units, price = _calc_units(72.0, 18.0, 300.0, 2.0, 50.0, poly_ask_levels=[])
        assert units == 0
        assert price == 0.0

    def test_book_walk_insufficient_total_depth(self):
        # Even walking the whole book doesn't reach minimum
        levels = [(18.0, 2.0), (20.0, 2.0)]  # total 4, need 6
        units, price = _calc_units(72.0, 18.0, 300.0, 2.0, 50.0, poly_ask_levels=levels)
        assert units == 0
        assert price == 0.0


# ---------------------------------------------------------------------------
# execute — happy path
# ---------------------------------------------------------------------------

class TestExecuteHappyPath:
    def test_strategy_a_fills_both_legs(self):
        """Both legs fill → status='filled', correct order IDs returned."""
        executor, k_trader, p_trader = _make_executor(max_trade_usd=5.0)
        opp = _make_opportunity(kalshi_side="YES", poly_side="NO", k_cost=55.0, p_cost=40.0)

        k_trader.place_order.return_value = {"order": {"order_id": "k-order-1"}}
        p_trader.place_order.return_value = {"orderID": "p-order-1"}

        result = executor.execute(opp)

        assert result.status == "filled"
        assert result.kalshi_order_id == "k-order-1"
        assert result.poly_order_id == "p-order-1"
        assert result.units == 5

    def test_strategy_b_buys_no_on_kalshi_yes_on_poly(self):
        """Strategy B: Kalshi NO + Polymarket YES."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity(kalshi_side="NO", poly_side="YES", k_cost=47.0, p_cost=48.0)

        k_trader.place_order.return_value = {"order": {"order_id": "k-b"}}
        p_trader.place_order.return_value = {"orderID": "p-b"}

        result = executor.execute(opp)
        assert result.status == "filled"

        # Kalshi should be called with side="no"
        k_call = k_trader.place_order.call_args
        assert k_call.kwargs["side"] == "no"

    def test_correct_poly_token_selected_for_yes(self):
        """poly_side=YES → use yes_token_id for Polymarket order."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity(kalshi_side="NO", poly_side="YES")

        k_trader.place_order.return_value = {"order": {"order_id": "k-1"}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        executor.execute(opp)
        p_call = p_trader.place_order.call_args
        assert p_call.kwargs["token_id"] == opp.pair.poly.yes_token_id

    def test_correct_poly_token_selected_for_no(self):
        """poly_side=NO → use no_token_id for Polymarket order."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity(kalshi_side="YES", poly_side="NO")

        k_trader.place_order.return_value = {"order": {"order_id": "k-1"}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        executor.execute(opp)
        p_call = p_trader.place_order.call_args
        assert p_call.kwargs["token_id"] == opp.pair.poly.no_token_id

    def test_poly_price_converted_to_fraction(self):
        """Polymarket receives price in 0-1 range, not cents."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity(k_cost=55.0, p_cost=40.0)

        k_trader.place_order.return_value = {"order": {"order_id": "k-1"}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        executor.execute(opp)
        p_call = p_trader.place_order.call_args
        assert abs(p_call.kwargs["price"] - 0.40) < 1e-9

    def test_profit_calculation(self):
        """guaranteed_profit_usd = units × spread_cents / 100."""
        executor, k_trader, p_trader = _make_executor()
        # spread = 100 - 55 - 40 = 5c, 5 units → $0.25
        opp = _make_opportunity(k_cost=55.0, p_cost=40.0)

        k_trader.place_order.return_value = {"order": {"order_id": "k-1"}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        result = executor.execute(opp)
        assert result.units == 5
        assert abs(result.guaranteed_profit_usd - 0.25) < 0.001


# ---------------------------------------------------------------------------
# execute — failure paths
# ---------------------------------------------------------------------------

class TestExecuteFailurePaths:
    def test_poly_insufficient_balance_returns_skipped(self):
        """If Poly wallet balance < $1, skip before placing any order."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()
        p_trader.get_usdc_balance.return_value = 0.50   # below $1 minimum

        result = executor.execute(opp)

        assert result.status == "skipped"
        assert result.reason == "poly_insufficient_balance"
        k_trader.place_order.assert_not_called()
        p_trader.place_order.assert_not_called()

    def test_poly_balance_check_exception_returns_skipped(self):
        """If Poly balance check throws, skip safely without placing any order."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()
        p_trader.get_usdc_balance.side_effect = Exception("network error")

        result = executor.execute(opp)

        assert result.status == "skipped"
        assert result.reason == "poly_balance_check_failed"
        k_trader.place_order.assert_not_called()

    def test_sufficient_poly_balance_proceeds(self):
        """If Poly wallet balance >= $1, execution proceeds normally."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()
        p_trader.get_usdc_balance.return_value = 10.0   # well above minimum
        k_trader.place_order.return_value = {"order": {"order_id": "k-1"}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        result = executor.execute(opp)
        assert result.status == "filled"

    def test_insufficient_units_returns_skipped(self):
        """If calculated units < 1, return skipped."""
        executor, k_trader, p_trader = _make_executor(max_trade_usd=0.01)
        p_trader.get_usdc_balance.return_value = 10.0   # balance OK, units will be 0
        # Very tiny budget → 0 units
        opp = _make_opportunity(k_cost=55.0, p_cost=40.0)
        result = executor.execute(opp)
        assert result.status == "skipped"
        assert result.reason == "insufficient_units"
        k_trader.place_order.assert_not_called()

    def test_missing_poly_token_returns_error(self):
        """If poly token ID is None, return error without placing any order."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity(poly_side="NO")
        # Remove the no_token_id
        opp.pair.poly.no_token_id = None

        result = executor.execute(opp)
        assert result.status == "error"
        assert result.reason == "missing_poly_token_id"
        k_trader.place_order.assert_not_called()

    def test_kalshi_leg_failure_returns_skipped(self):
        """If Kalshi order raises, return skipped without touching Polymarket."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()
        k_trader.place_order.side_effect = Exception("Kalshi API error")

        result = executor.execute(opp)
        assert result.status == "skipped"
        assert result.reason == "kalshi_leg_failed"
        p_trader.place_order.assert_not_called()

    def test_poly_failure_triggers_unwind(self):
        """If Polymarket leg raises after Kalshi fills, attempt to unwind Kalshi."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()

        k_trader.place_order.return_value = {"order": {"order_id": "k-1"}}
        p_trader.place_order.side_effect = Exception("Poly API error")

        # Mock successful unwind
        k_trader.get_market_price.return_value = {"yes_bid": 52.0, "no_bid": 48.0}
        k_trader.place_order.side_effect = [
            {"order": {"order_id": "k-1"}},   # first call: buy succeeds
            {"order": {"order_id": "k-unwind"}},  # second call: sell succeeds
        ]

        result = executor.execute(opp)
        assert result.status == "unwound"
        assert result.reason == "poly_0_fill"
        assert result.kalshi_order_id == "k-1"

    def test_poly_fok_zero_fill_unwinds_kalshi(self):
        """If Poly FOK order gets 0 fill, Kalshi must be unwound — naked position prevented."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()

        # Poly order is placed (no exception), gets an order_id, but 0 shares filled
        k_trader.place_order.side_effect = [
            {"order": {"order_id": "k-1"}},    # Kalshi buy succeeds
            {"order": {"order_id": "k-unwind"}},  # Kalshi unwind sell
        ]
        p_trader.place_order.return_value = {"orderID": "p-1"}
        # Override side_effect so return_value = 0.0 takes effect
        p_trader.get_actual_fill.side_effect = None
        p_trader.get_actual_fill.return_value = 0.0  # FOK killed — 0 fill

        k_trader.get_market_price.return_value = {"yes_bid": 52.0, "no_bid": 48.0}

        with patch("scanner.arb_executor.time.sleep"):
            result = executor.execute(opp)

        assert result.status == "unwound"
        assert result.reason == "poly_0_fill"
        assert result.poly_order_id == "p-1"   # order ID was recorded even on 0-fill

    def test_poly_partial_fill_adjusts_units(self):
        """If Poly fills fewer shares than Kalshi, record partial (hedged) amount."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity(k_depth=100.0, p_depth=100.0)

        k_trader.place_order.return_value = {"order": {"order_id": "k-1"}}
        p_trader.place_order.return_value = {"orderID": "p-1"}
        # Poly only filled 3 out of 5 requested shares — override side_effect
        p_trader.get_actual_fill.side_effect = None
        p_trader.get_actual_fill.return_value = 3.0

        result = executor.execute(opp)
        assert result.status == "filled"
        assert result.units == 3   # aligned down to actual poly fill

    def test_failed_unwind_returns_partial_stuck(self):
        """If both Poly leg and Kalshi unwind fail, status is partial_stuck."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()

        k_trader.place_order.side_effect = [
            {"order": {"order_id": "k-1"}},  # buy succeeds
            Exception("sell also fails"),     # unwind fails
            Exception("retry 1 fails"),
            Exception("retry 2 fails"),
        ]
        p_trader.place_order.side_effect = Exception("Poly error")
        k_trader.get_market_price.return_value = {"yes_bid": 52.0, "no_bid": 48.0}

        with patch("scanner.arb_executor.time.sleep"):  # skip delays
            result = executor.execute(opp)
        assert result.status == "partial_stuck"


# ---------------------------------------------------------------------------
# Cooldowns
# ---------------------------------------------------------------------------

class TestCooldowns:
    def test_not_on_cooldown_initially(self):
        executor, _, _ = _make_executor()
        opp = _make_opportunity()
        assert not executor.is_on_cooldown(opp)

    def test_on_cooldown_after_successful_trade(self):
        """After a successful trade, the pair should be on cooldown."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()

        k_trader.place_order.return_value = {"order": {"order_id": "k-1"}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        executor.tick()  # cycle = 1
        result = executor.execute(opp)
        assert result.status == "filled"

        # Should be on cooldown now
        assert executor.is_on_cooldown(opp)

    def test_cooldown_expires_after_enough_ticks(self):
        """Cooldown should expire after EXEC_COOLDOWN_CYCLES ticks."""
        from scanner.config import EXEC_COOLDOWN_CYCLES

        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()

        k_trader.place_order.return_value = {"order": {"order_id": "k-1"}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        executor.tick()  # cycle = 1
        executor.execute(opp)

        # Advance enough cycles to exit cooldown
        for _ in range(EXEC_COOLDOWN_CYCLES + 1):
            executor.tick()

        assert not executor.is_on_cooldown(opp)


# ---------------------------------------------------------------------------
# Partial Kalshi fill
# ---------------------------------------------------------------------------

class TestPartialKalshiFill:
    def test_partial_fill_sizes_poly_to_actual_fill(self):
        """If Kalshi fills 3 of 5 contracts, Poly order should be placed for 3."""
        executor, k_trader, p_trader = _make_executor(max_trade_usd=5.0)
        opp = _make_opportunity(k_cost=55.0, p_cost=40.0)

        k_trader.place_order.return_value = {"order": {"order_id": "k-partial"}}
        # fill_count=3 (actual fills), remaining_count=2 (still resting)
        k_trader.get_order.return_value = {"order": {"status": "resting", "fill_count": 3, "remaining_count": 2}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        result = executor.execute(opp)

        assert result.status == "filled"
        assert result.units == 3
        p_call = p_trader.place_order.call_args
        assert p_call.kwargs["size"] == 3.0

    def test_partial_fill_cancels_resting_remainder(self):
        """Resting remainder on Kalshi should be cancelled to avoid future unhedged fill."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()

        k_trader.place_order.return_value = {"order": {"order_id": "k-partial"}}
        # fill_count=4, remaining_count=1 — partial fill with resting order
        k_trader.get_order.return_value = {"order": {"status": "resting", "fill_count": 4, "remaining_count": 1}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        executor.execute(opp)

        k_trader.cancel_order.assert_called_once_with("k-partial")

    def test_zero_fill_returns_skipped(self):
        """If Kalshi fills 0 contracts (order cancelled), skip without placing Poly order."""
        executor, k_trader, p_trader = _make_executor(max_trade_usd=5.0)
        opp = _make_opportunity(k_cost=55.0, p_cost=40.0)

        k_trader.place_order.return_value = {"order": {"order_id": "k-zero"}}
        # status=canceled, fill_count=0 — this is the real Kalshi 0-fill scenario
        k_trader.get_order.return_value = {"order": {"status": "canceled", "fill_count": 0, "remaining_count": 5}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        result = executor.execute(opp)

        assert result.status == "skipped"
        assert result.reason == "kalshi_no_fill"
        p_trader.place_order.assert_not_called()

    def test_full_fill_does_not_cancel(self):
        """If Kalshi fills all contracts, cancel_order should not be called."""
        executor, k_trader, p_trader = _make_executor()
        opp = _make_opportunity()

        k_trader.place_order.return_value = {"order": {"order_id": "k-full"}}
        k_trader.get_order.return_value = {"order": {"status": "filled", "fill_count": 5, "remaining_count": 0}}
        p_trader.place_order.return_value = {"orderID": "p-1"}

        executor.execute(opp)

        k_trader.cancel_order.assert_not_called()

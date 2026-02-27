"""Tests for the PaperArbExecutor (dry-run / paper trading mode)."""

from datetime import datetime, timedelta, timezone

import pytest

from scanner.models import MarketType, MatchedPair, NormalizedMarket, Platform
from scanner.paper_executor import PaperArbExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_opp(
    k_yes_ask: float = 51.0,
    p_no_ask: float = 40.0,
    k_depth: float = 200.0,
    p_depth: float = 200.0,
    k_side: str = "YES",
    p_side: str = "NO",
    spread_override: float | None = None,
):
    """Build a minimal Opportunity ready for PaperArbExecutor.execute()."""
    from scanner.models import Opportunity

    close = datetime.now(timezone.utc) + timedelta(hours=4)
    km = NormalizedMarket(
        platform=Platform.KALSHI,
        platform_id="TEST-TICK",
        platform_url="https://kalshi.com/markets/TEST-TICK",
        raw_question="Test team A vs team B Map 1",
        market_type=MarketType.SPORTS,
        asset="CS2", direction="WIN",
        team="Team A", opponent="Team B", sport="CS2",
        resolution_dt=close,
        yes_ask_cents=k_yes_ask, no_ask_cents=100.0 - k_yes_ask,
        yes_ask_depth=k_depth, no_ask_depth=k_depth,
    )
    pm = NormalizedMarket(
        platform=Platform.POLYMARKET,
        platform_id="poly-test-abc",
        platform_url="https://polymarket.com/event/test",
        raw_question="Test team A vs B",
        market_type=MarketType.SPORTS,
        asset="CS2", direction="WIN",
        team="Team A", opponent="Team B", sport="CS2",
        resolution_dt=close,
        yes_ask_cents=p_no_ask, no_ask_cents=p_no_ask,
        yes_ask_depth=p_depth, no_ask_depth=p_depth,
        yes_token_id="tok-yes-abc",
        no_token_id="tok-no-abc",
    )
    pair = MatchedPair(kalshi=km, poly=pm)
    combined = k_yes_ask + p_no_ask
    spread = spread_override if spread_override is not None else round(100.0 - combined, 2)
    return Opportunity(
        pair=pair,
        kalshi_side=k_side,
        poly_side=p_side,
        kalshi_cost_cents=k_yes_ask,
        poly_cost_cents=p_no_ask,
        combined_cost_cents=combined,
        spread_cents=spread,
        tier="High",
        hours_to_close=4.0,
        detected_at=datetime.now(timezone.utc),
        kalshi_depth_shares=k_depth,
        poly_depth_shares=p_depth,
    )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

class TestInit:
    def test_default_capital_split(self):
        ex = PaperArbExecutor(total_capital=10_000.0)
        assert ex._wallet.kalshi_balance == 5_000.0
        assert ex._wallet.poly_balance == 5_000.0

    def test_custom_capital(self):
        ex = PaperArbExecutor(total_capital=20_000.0, kalshi_ratio=0.6)
        assert ex._wallet.kalshi_balance == 12_000.0
        assert ex._wallet.poly_balance == 8_000.0

    def test_trade_count_starts_zero(self):
        assert PaperArbExecutor()._wallet.trade_count == 0


# ---------------------------------------------------------------------------
# Cooldown (mirrors real executor behaviour)
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_not_on_cooldown_initially(self):
        ex = PaperArbExecutor()
        opp = _make_opp()
        assert ex.is_on_cooldown(opp) is False

    def test_on_cooldown_after_trade(self):
        ex = PaperArbExecutor()
        opp = _make_opp()
        ex.execute(opp)
        assert ex.is_on_cooldown(opp) is True

    def test_cooldown_expires_after_ticks(self):
        from scanner.config import EXEC_COOLDOWN_CYCLES
        ex = PaperArbExecutor()
        opp = _make_opp()
        ex.execute(opp)
        for _ in range(EXEC_COOLDOWN_CYCLES + 1):
            ex.tick()
        assert ex.is_on_cooldown(opp) is False


# ---------------------------------------------------------------------------
# Execute — happy path
# ---------------------------------------------------------------------------

class TestExecuteHappyPath:
    def setup_method(self):
        self.ex = PaperArbExecutor(total_capital=10_000.0, max_trade_usd=50.0)

    def test_status_is_filled(self):
        result = self.ex.execute(_make_opp())
        assert result.status == "filled"

    def test_paper_order_ids(self):
        result = self.ex.execute(_make_opp())
        assert result.kalshi_order_id.startswith("PAPER-K-")
        assert result.poly_order_id.startswith("PAPER-P-")

    def test_units_positive(self):
        result = self.ex.execute(_make_opp())
        assert result.units > 0

    def test_costs_computed_correctly(self):
        # 51c Kalshi + 40c Poly = 91c combined → 9c spread
        result = self.ex.execute(_make_opp(k_yes_ask=51.0, p_no_ask=40.0))
        # At $50 max, combined 91c → max 54 units; depth 200 → 54 units
        expected_units = result.units
        assert abs(result.kalshi_cost_usd - expected_units * 0.51) < 0.01
        assert abs(result.poly_cost_usd   - expected_units * 0.40) < 0.01

    def test_guaranteed_profit_equals_units_times_spread(self):
        opp = _make_opp(k_yes_ask=51.0, p_no_ask=40.0)  # spread=9c
        result = self.ex.execute(opp)
        expected = round(result.units * 9.0 / 100.0, 4)
        assert abs(result.guaranteed_profit_usd - expected) < 0.0001

    def test_wallet_balances_decrease_after_trade(self):
        k_before = self.ex._wallet.kalshi_balance
        p_before = self.ex._wallet.poly_balance
        result = self.ex.execute(_make_opp())
        assert self.ex._wallet.kalshi_balance < k_before
        assert self.ex._wallet.poly_balance < p_before
        assert abs(self.ex._wallet.kalshi_balance - (k_before - result.kalshi_cost_usd)) < 0.01

    def test_trade_count_increments(self):
        self.ex.execute(_make_opp())
        assert self.ex._wallet.trade_count == 1
        self.ex.execute(_make_opp())
        assert self.ex._wallet.trade_count == 2

    def test_total_invested_accumulates(self):
        r1 = self.ex.execute(_make_opp())
        r2 = self.ex.execute(_make_opp())
        expected = round(r1.total_cost_usd + r2.total_cost_usd, 4)
        assert abs(self.ex._wallet.total_invested - expected) < 0.01

    def test_fee_is_175pct_of_kalshi_units(self):
        from scanner.config import KALSHI_TAKER_FEE_RATE
        result = self.ex.execute(_make_opp())
        expected_fee = round(result.units * KALSHI_TAKER_FEE_RATE, 4)
        assert abs(self.ex._wallet.total_kalshi_fees - expected_fee) < 0.0001


# ---------------------------------------------------------------------------
# Execute — edge cases / skips
# ---------------------------------------------------------------------------

class TestExecuteSkips:
    def test_skip_when_poly_balance_exhausted(self):
        ex = PaperArbExecutor(total_capital=1.0)   # tiny wallet
        # After a few trades the poly balance < $1 min
        for _ in range(5):
            ex.execute(_make_opp())
        # Eventually should hit poly_insufficient_balance
        results = [ex.execute(_make_opp()) for _ in range(20)]
        skip_results = [r for r in results if r.status == "skipped"]
        assert any(r.reason == "poly_insufficient_balance" for r in skip_results)

    def test_skip_when_no_token_id(self):
        opp = _make_opp()
        # Remove token IDs
        opp.pair.poly.yes_token_id = None
        opp.pair.poly.no_token_id = None
        ex = PaperArbExecutor()
        result = ex.execute(opp)
        assert result.status == "skipped"
        assert result.reason == "missing_poly_token_id"

    def test_depth_caps_units(self):
        # Only 3 contracts available at ask
        result = PaperArbExecutor(max_trade_usd=500.0).execute(
            _make_opp(k_depth=3.0, p_depth=3.0)
        )
        assert result.units <= 3


# ---------------------------------------------------------------------------
# Best / worst tracking
# ---------------------------------------------------------------------------

class TestBestWorstTracking:
    def test_best_profit_tracked(self):
        ex = PaperArbExecutor(total_capital=50_000.0, max_trade_usd=500.0)
        ex.execute(_make_opp(k_yes_ask=51.0, p_no_ask=40.0))  # 9c spread (bigger profit)
        ex.execute(_make_opp(k_yes_ask=55.0, p_no_ask=40.0))  # 5c spread (smaller profit)
        assert ex._wallet.best_profit >= ex._wallet.worst_profit

    def test_worst_profit_not_inf_after_trade(self):
        ex = PaperArbExecutor()
        ex.execute(_make_opp())
        assert ex._wallet.worst_profit < float("inf")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_contains_key_sections(self):
        ex = PaperArbExecutor(total_capital=10_000.0)
        ex.execute(_make_opp())
        rpt = ex.report()
        assert "PAPER TRADING REPORT" in rpt
        assert "Initial capital" in rpt
        assert "Net profit" in rpt
        assert "Trades simulated" in rpt
        assert "Best trade" in rpt

    def test_report_shows_correct_trade_count(self):
        ex = PaperArbExecutor()
        ex.execute(_make_opp())
        ex.execute(_make_opp())
        assert "2" in ex.report()

    def test_report_zero_trades(self):
        ex = PaperArbExecutor()
        rpt = ex.report()
        assert "PAPER TRADING REPORT" in rpt
        assert "Best trade" not in rpt   # only shown when trades > 0

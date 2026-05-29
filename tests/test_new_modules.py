"""Tests for new modules: drawdown, portfolio risk, timeline, arbitrage,
position manager, fill tracker, audit trail, cache, rate limiter.
"""

from __future__ import annotations

import time
import pytest

# ─── Drawdown Manager ─────────────────────────────────────────────────

from src.policy.drawdown import DrawdownManager, DrawdownState


class TestDrawdownManager:
    def _make_manager(self, equity: float = 10000) -> DrawdownManager:
        from src.config import load_config
        cfg = load_config()
        return DrawdownManager(equity, config=cfg)

    def test_initial_state(self) -> None:
        mgr = self._make_manager(10000)
        assert mgr.state.peak_equity == 10000
        assert mgr.state.current_equity == 10000
        assert mgr.state.drawdown_pct == 0.0
        assert mgr.state.heat_level == 0
        assert mgr.state.is_killed is False

    def test_equity_increase_updates_peak(self) -> None:
        mgr = self._make_manager(10000)
        mgr.update(11000)
        assert mgr.state.peak_equity == 11000
        assert mgr.state.drawdown_pct == 0.0

    def test_drawdown_calculation(self) -> None:
        mgr = self._make_manager(10000)
        mgr.update(9500)
        assert mgr.state.drawdown_pct == pytest.approx(0.05, abs=0.001)

    def test_heat_level_increases(self) -> None:
        mgr = self._make_manager(10000)
        mgr.update(8800)  # 12% drawdown → above warning_drawdown_pct (0.10)
        assert mgr.state.heat_level >= 1
        assert mgr.state.kelly_multiplier < 1.0

    def test_kill_switch_triggers(self) -> None:
        mgr = self._make_manager(10000)
        mgr.update(7400)  # 26% drawdown → kill switch
        assert mgr.state.is_killed is True
        assert mgr.state.kelly_multiplier == 0.0

    def test_can_trade_when_healthy(self) -> None:
        mgr = self._make_manager(10000)
        can, reason = mgr.can_trade()
        assert can is True

    def test_cannot_trade_when_killed(self) -> None:
        mgr = self._make_manager(10000)
        mgr.update(7000)
        can, reason = mgr.can_trade()
        assert can is False

    def test_reset_kill_switch(self) -> None:
        mgr = self._make_manager(10000)
        mgr.update(7000)
        assert mgr.state.is_killed is True
        # Recover equity above kill threshold before resetting
        mgr.state.current_equity = 9000
        mgr.state.drawdown_pct = 0.10
        mgr.reset_kill_switch()
        assert mgr.state.is_killed is False


# ─── Portfolio Risk ──────────────────────────────────────────────────

from src.policy.portfolio_risk import PortfolioRiskManager, PositionSnapshot


class TestPortfolioRisk:
    def _make_manager(self, bankroll: float = 10000) -> PortfolioRiskManager:
        from src.config import load_config
        return PortfolioRiskManager(bankroll, load_config())

    def _make_position(self, **overrides) -> PositionSnapshot:
        defaults = dict(
            market_id="m1", question="Test", category="MACRO",
            event_slug="test-event", side="YES", size_usd=500,
            entry_price=0.60, current_price=0.65,
        )
        defaults.update(overrides)
        return PositionSnapshot(**defaults)

    def test_empty_portfolio_is_healthy(self) -> None:
        mgr = self._make_manager()
        report = mgr.assess([])
        assert report.is_healthy is True
        assert report.num_positions == 0

    def test_single_position_healthy(self) -> None:
        mgr = self._make_manager(10000)
        pos = self._make_position(size_usd=500)
        report = mgr.assess([pos])
        assert report.is_healthy is True
        assert report.total_exposure_usd == 500

    def test_category_violation(self) -> None:
        mgr = self._make_manager(10000)
        # 4 positions in same category, 1000 each = 40% > 30% limit
        positions = [
            self._make_position(market_id=f"m{i}", size_usd=1000)
            for i in range(4)
        ]
        report = mgr.assess(positions)
        assert report.is_healthy is False
        assert len(report.category_violations) > 0

    def test_can_add_position(self) -> None:
        mgr = self._make_manager(10000)
        positions = [self._make_position(size_usd=500)]
        ok, reason = mgr.can_add_position(positions, "MACRO", "test-event", 500)
        assert ok is True

    def test_cannot_exceed_category_limit(self) -> None:
        mgr = self._make_manager(10000)
        positions = [
            self._make_position(market_id=f"m{i}", size_usd=1000)
            for i in range(3)
        ]
        # Would bring MACRO to 4000/10000 = 40% > 30%
        ok, reason = mgr.can_add_position(positions, "MACRO", "new-event", 1000)
        assert ok is False
        assert "category" in reason.lower() or "Category" in reason


# ─── Timeline Intelligence ───────────────────────────────────────────

import datetime as dt
from src.policy.timeline import assess_timeline


class TestTimeline:
    def test_no_end_date(self) -> None:
        result = assess_timeline("m1", None)
        assert result.phase == "unknown"
        assert result.urgency_score == 0.0
        assert result.sizing_multiplier == 1.0

    def test_early_market(self) -> None:
        end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90)
        result = assess_timeline("m1", end)
        assert result.phase == "early"
        assert result.sizing_multiplier < 1.0  # penalised

    def test_mid_market(self) -> None:
        end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=20)
        result = assess_timeline("m1", end)
        assert result.phase == "mid"
        assert result.sizing_multiplier == 1.0

    def test_late_market(self) -> None:
        end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)
        result = assess_timeline("m1", end)
        assert result.phase == "late"
        assert result.sizing_multiplier >= 1.0

    def test_endgame_market(self) -> None:
        """With hold-to-resolution strategy, near-expiry markets stay 'late' and never force exit."""
        end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=6)
        result = assess_timeline("m1", end)
        assert result.phase == "late"  # No endgame since exit_before_resolution_hours=0
        assert result.should_exit_before is False  # Hold through resolution
        assert result.sizing_multiplier >= 1.0


# ─── Arbitrage Detection ────────────────────────────────────────────

from src.policy.arbitrage import detect_arbitrage
from src.connectors.polymarket_gamma import GammaMarket, GammaToken


class TestArbitrage:
    def _make_market(self, id: str, question: str, yes_price: float, slug: str = "") -> GammaMarket:
        return GammaMarket(
            id=id,
            question=question,
            slug=slug or id,
            tokens=[
                GammaToken(token_id=f"{id}_yes", outcome="Yes", price=yes_price),
                GammaToken(token_id=f"{id}_no", outcome="No", price=round(1 - yes_price, 2)),
            ],
            volume=10000,
            liquidity=5000,
        )

    def test_no_opportunities_with_single_market(self) -> None:
        markets = [self._make_market("m1", "Test?", 0.50)]
        opps = detect_arbitrage(markets)
        assert len(opps) == 0

    def test_multi_outcome_sum_deviation(self) -> None:
        # Market with 3 tokens that don't sum to 1.0
        m = GammaMarket(
            id="multi1",
            question="Who wins?",
            slug="who-wins",
            tokens=[
                GammaToken(token_id="t1", outcome="A", price=0.40),
                GammaToken(token_id="t2", outcome="B", price=0.30),
                GammaToken(token_id="t3", outcome="C", price=0.40),
            ],
            volume=10000,
            liquidity=5000,
        )
        opps = detect_arbitrage([m])
        # Sum = 1.10, deviation = 0.10, fee_cost ~ 0.04
        # edge = 0.10 - 0.04 = 0.06 > 0.01
        assert len(opps) >= 1
        assert opps[0].arb_type == "multi_outcome"


# ─── Position Manager ───────────────────────────────────────────────

from src.engine.position_manager import PositionManager


class TestPositionManager:
    def test_open_and_close_position(self) -> None:
        mgr = PositionManager()
        pos = mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.60,
        )
        assert pos.market_id == "m1"
        assert pos.status == "open"
        assert "m1" in mgr.positions

        closed = mgr.close_position("m1", exit_price=0.70, reason="take_profit")
        assert closed is not None
        assert closed.status == "closed"
        assert closed.realised_pnl > 0
        assert "m1" not in mgr.positions
        assert len(mgr.closed_positions) == 1

    def test_update_price(self) -> None:
        mgr = PositionManager()
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.50,
        )
        mgr.update_price("m1", 0.60)
        pos = mgr.positions["m1"]
        assert pos.current_price == 0.60
        assert pos.unrealised_pnl > 0

    def test_stop_loss_signal(self) -> None:
        mgr = PositionManager(stop_loss_pct=0.20)
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.50,
        )
        mgr.update_price("m1", 0.35)  # Below stop loss
        signals = mgr.check_exits()
        assert any(s.reason == "stop_loss" for s in signals)

    def test_take_profit_signal(self) -> None:
        """TP only fires at resolution price: 0.99 for YES, 0.01 for NO."""
        mgr = PositionManager()
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.50,
        )
        # 0.80 should NOT trigger TP (hold to resolution)
        mgr.update_price("m1", 0.80)
        signals = mgr.check_exits()
        assert not any(s.reason == "take_profit" for s in signals)

        # 0.99 should trigger TP (market resolved / 100%)
        mgr.update_price("m1", 0.99)
        signals = mgr.check_exits()
        assert any(s.reason == "take_profit" for s in signals)

    def test_take_profit_no_side(self) -> None:
        """TP for NO position fires at 0.01 (resolution price)."""
        mgr = PositionManager()
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="NO", size_usd=100, entry_price=0.60,
        )
        # 0.30 should NOT trigger TP
        mgr.update_price("m1", 0.30)
        signals = mgr.check_exits()
        assert not any(s.reason == "take_profit" for s in signals)

        # 0.01 should trigger TP (market resolved for NO)
        mgr.update_price("m1", 0.01)
        signals = mgr.check_exits()
        assert any(s.reason == "take_profit" for s in signals)

    def test_kill_switch_exits_all(self) -> None:
        mgr = PositionManager()
        mgr.open_position("m1", "Q1?", "MACRO", "e1", "YES", 100, 0.50)
        mgr.open_position("m2", "Q2?", "MACRO", "e2", "YES", 200, 0.60)
        signals = mgr.check_exits(force_exit_all=True)
        assert len(signals) == 2
        assert all(s.reason == "kill_switch" for s in signals)

    def test_total_pnl(self) -> None:
        mgr = PositionManager()
        mgr.open_position("m1", "Q?", "MACRO", "e1", "YES", 100, 0.50)
        mgr.update_price("m1", 0.60)
        assert mgr.total_unrealised_pnl() > 0


# ─── Fill Tracker ─────────────────────────────────────────────────

from src.execution.fill_tracker import FillTracker


class TestFillTracker:
    def test_register_and_record_fill(self) -> None:
        tracker = FillTracker()
        tracker.register_order("o1", "m1", 0.60, 100, "simple")
        fill = tracker.record_fill("o1", 0.61, 100)
        assert fill is not None
        assert fill.slippage_bps > 0
        assert fill.fill_rate == pytest.approx(1.0, abs=0.01)

    def test_partial_fill(self) -> None:
        tracker = FillTracker()
        tracker.register_order("o1", "m1", 0.60, 100, "simple")
        fill = tracker.record_fill("o1", 0.60, 50)
        assert fill.is_partial is True
        assert fill.fill_rate == pytest.approx(0.5, abs=0.01)

    def test_unfilled_order(self) -> None:
        tracker = FillTracker()
        tracker.register_order("o1", "m1", 0.60, 100, "simple")
        tracker.record_unfilled("o1")
        quality = tracker.get_quality()
        assert quality.unfilled >= 1


# ─── Audit Trail ──────────────────────────────────────────────────

from src.storage.audit import AuditTrail


class TestAuditTrail:
    def test_record_entry(self) -> None:
        trail = AuditTrail()
        entry = trail.record("m1", "TRADE", "decision", {"edge": 0.05})
        assert entry.market_id == "m1"
        assert entry.decision == "TRADE"
        assert entry.checksum != ""

    def test_integrity_check(self) -> None:
        trail = AuditTrail()
        entry = trail.record("m1", "TRADE", "decision", {"edge": 0.05})
        assert entry.verify_integrity() is True

    def test_query_entries(self) -> None:
        trail = AuditTrail()
        trail.record("m1", "TRADE", "decision")
        trail.record("m2", "NO_TRADE", "decision")
        trail.record("m1", "FILL", "fill")

        trades = trail.get_entries(decision="TRADE")
        assert len(trades) == 1
        m1_entries = trail.get_entries(market_id="m1")
        assert len(m1_entries) == 2

    def test_verify_all(self) -> None:
        trail = AuditTrail()
        for i in range(10):
            trail.record(f"m{i}", "TRADE", "decision")
        valid, invalid = trail.verify_all()
        assert valid == 10
        assert invalid == 0


# ─── Cache ──────────────────────────────────────────────────────────

from src.storage.cache import TTLCache, get_cache


class TestCache:
    def test_set_and_get(self) -> None:
        cache = TTLCache()
        cache.put("key1", "value1", ttl_secs=60)
        assert cache.get("key1") == "value1"

    def test_expired_entry(self) -> None:
        cache = TTLCache()
        cache.put("key1", "value1", ttl_secs=0.01)
        time.sleep(0.02)
        assert cache.get("key1") is None

    def test_invalidate(self) -> None:
        cache = TTLCache()
        cache.put("key1", "value1", ttl_secs=60)
        cache.invalidate("key1")
        assert cache.get("key1") is None

    def test_get_cache_domain(self) -> None:
        cache = get_cache("test_search")
        cache.put("q1", {"results": [1, 2, 3]}, ttl_secs=60)
        assert cache.get("q1") == {"results": [1, 2, 3]}

    def test_get_cache_orderbook(self) -> None:
        cache = get_cache("test_orderbook")
        cache.put("tok1", {"bids": []}, ttl_secs=30)
        assert cache.get("tok1") == {"bids": []}


# ─── Rate Limiter ────────────────────────────────────────────────────

from src.connectors.rate_limiter import TokenBucket, BucketConfig, RateLimiterRegistry
import asyncio


class TestRateLimiter:
    def test_token_bucket_creation(self) -> None:
        config = BucketConfig(tokens_per_second=10.0, max_burst=10)
        limiter = TokenBucket(config)
        assert limiter.try_acquire() is True

    def test_registry_returns_limiter(self) -> None:
        registry = RateLimiterRegistry()
        limiter = registry.get("openai")
        assert limiter is not None
        assert isinstance(limiter, TokenBucket)

    def test_registry_default_limiter(self) -> None:
        registry = RateLimiterRegistry()
        limiter = registry.get("unknown_service")
        assert limiter is not None

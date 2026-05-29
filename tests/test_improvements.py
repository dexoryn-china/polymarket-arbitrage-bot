"""Tests for the improvement round features:
  - CostTracker (API cost tracking)
  - Dynamic stop-loss (edge/confidence based)
  - Trailing stop-loss
  - Partial exits
  - Resolution detection
  - FallbackSearchProvider
  - Portfolio correlation check
  - PipelineContext
  - Rebalancing signals
  - Calibrator (unit-level)
  - Ensemble adaptive weights
"""

from __future__ import annotations

import asyncio
import pytest

# ─── CostTracker ─────────────────────────────────────────────────────

from src.observability.metrics import CostTracker


class TestCostTracker:
    def test_record_call_increments_counts(self) -> None:
        ct = CostTracker(cost_map={"openai": 0.01, "serpapi": 0.002})
        ct.record_call("openai")
        ct.record_call("openai")
        ct.record_call("serpapi")
        snap = ct.snapshot()
        assert snap["total_calls"]["openai"] == 2
        assert snap["total_calls"]["serpapi"] == 1

    def test_cost_calculation(self) -> None:
        ct = CostTracker(cost_map={"openai": 0.01})
        ct.record_call("openai", count=5)
        snap = ct.snapshot()
        assert snap["cycle_cost_usd"] == pytest.approx(0.05, abs=1e-6)

    def test_end_cycle_resets_cycle_but_keeps_total(self) -> None:
        ct = CostTracker(cost_map={"openai": 0.01})
        ct.record_call("openai", count=3)
        summary = ct.end_cycle()
        assert summary["cycle_cost_usd"] == pytest.approx(0.03, abs=1e-6)
        assert summary["total_cost_usd"] == pytest.approx(0.03, abs=1e-6)

        # After end_cycle, cycle counters reset
        snap = ct.snapshot()
        assert snap["cycle_cost_usd"] == 0.0
        assert snap["cycle_calls"] == {}
        # Total persists
        assert snap["total_cost_usd"] == pytest.approx(0.03, abs=1e-6)

    def test_unknown_api_uses_default_cost(self) -> None:
        ct = CostTracker(cost_map={})
        ct.record_call("unknown_api")
        snap = ct.snapshot()
        assert snap["cycle_cost_usd"] == pytest.approx(0.001, abs=1e-6)

    def test_multiple_cycles(self) -> None:
        ct = CostTracker(cost_map={"a": 0.10})
        ct.record_call("a")
        ct.end_cycle()
        ct.record_call("a")
        ct.record_call("a")
        summary = ct.end_cycle()
        assert summary["cycle_cost_usd"] == pytest.approx(0.20, abs=1e-6)
        assert summary["total_cost_usd"] == pytest.approx(0.30, abs=1e-6)
        assert summary["total_calls"]["a"] == 3


# ─── Dynamic Stop-Loss / Trailing / Partial Exits ────────────────────

from src.engine.position_manager import PositionManager, ExitSignal


class TestDynamicStopLoss:
    def test_high_confidence_has_different_stop_than_low(self) -> None:
        """Different confidence levels produce different stop-loss prices."""
        mgr = PositionManager(stop_loss_pct=0.20)
        pos_high = mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.60,
            edge=0.10, confidence="HIGH",
        )
        mgr2 = PositionManager(stop_loss_pct=0.20)
        pos_low = mgr2.open_position(
            market_id="m2", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.60,
            edge=0.10, confidence="LOW",
        )
        # Different confidence => different stop-loss prices
        assert pos_high.stop_loss_price != pos_low.stop_loss_price

    def test_stop_loss_clamped_within_bounds(self) -> None:
        """Dynamic stop-loss is always between 8% and 35% of entry."""
        mgr = PositionManager(stop_loss_pct=0.20)
        pos = mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.60,
            edge=0.15, confidence="HIGH",
        )
        # Stop should be between entry*(1-0.35) and entry*(1-0.08)
        assert pos.stop_loss_price >= 0.60 * (1 - 0.35)
        assert pos.stop_loss_price <= 0.60 * (1 - 0.08)

    def test_edge_affects_stop_distance(self) -> None:
        """Higher edge => wider stop (more breathing room)."""
        mgr1 = PositionManager(stop_loss_pct=0.20)
        pos_low_edge = mgr1.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.60,
            edge=0.02, confidence="MEDIUM",
        )
        mgr2 = PositionManager(stop_loss_pct=0.20)
        pos_high_edge = mgr2.open_position(
            market_id="m2", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.60,
            edge=0.20, confidence="MEDIUM",
        )
        # Higher edge => wider stop => lower stop_loss_price for YES
        assert pos_high_edge.stop_loss_price < pos_low_edge.stop_loss_price


class TestTrailingStopLoss:
    def test_trailing_stop_activates_and_locks_gains(self) -> None:
        mgr = PositionManager(
            stop_loss_pct=0.20,
            trailing_stop_activation_pct=0.15,
            trailing_stop_distance_pct=0.10,
        )
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.50,
        )
        # Price rises well above activation threshold
        mgr.update_price("m1", 0.70)  # +40% gain
        # check_exits should evaluate trailing stop (but not trigger it yet
        # since there's no pullback)
        signals = mgr.check_exits()

        # Now simulate a pullback
        mgr.update_price("m1", 0.55)  # large pullback from 0.70
        signals = mgr.check_exits()
        trailing = [s for s in signals if s.reason == "trailing_stop"]
        assert len(trailing) >= 1

    def test_trailing_stop_does_not_fire_below_activation(self) -> None:
        mgr = PositionManager(
            stop_loss_pct=0.50,  # very wide stop to not interfere
            trailing_stop_activation_pct=0.30,
            trailing_stop_distance_pct=0.10,
        )
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.50,
        )
        # Small rise then drop — should not trigger trailing stop
        mgr.update_price("m1", 0.55)  # only +10% — below 30% activation
        mgr.check_exits()
        mgr.update_price("m1", 0.50)  # back to entry
        signals = mgr.check_exits()
        trailing = [s for s in signals if s.reason == "trailing_stop"]
        assert len(trailing) == 0


class TestPartialExits:
    def test_partial_exit_on_edge_narrowing(self) -> None:
        """Partial exit fires when model_probs are provided and edge narrows."""
        mgr = PositionManager(
            stop_loss_pct=0.20,
            partial_exit_threshold=0.50,  # exit when edge < 50% of entry
            partial_exit_fraction=0.50,
        )
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.50,
            edge=0.15,  # entry edge
        )
        # Price went up a bit (position is profitable)
        mgr.update_price("m1", 0.60)
        # Provide model_probs showing edge has narrowed from 0.15 to < 0.075
        # New model prob close to current price means edge narrowed
        signals = mgr.check_exits(model_probs={"m1": 0.62})
        # Edge = |0.62 - 0.60| = 0.02, which is < 50% of 0.15 = 0.075
        partial = [s for s in signals if s.reason == "partial_exit"]
        assert len(partial) == 1
        assert partial[0].exit_fraction == pytest.approx(0.50)

    def test_no_partial_exit_when_edge_still_wide(self) -> None:
        mgr = PositionManager(
            stop_loss_pct=0.20,
            partial_exit_threshold=0.50,
            partial_exit_fraction=0.50,
        )
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.50,
            edge=0.15,
        )
        mgr.update_price("m1", 0.60)
        # Edge still wide: |0.75 - 0.60| = 0.15 (100% of entry edge)
        signals = mgr.check_exits(model_probs={"m1": 0.75})
        partial = [s for s in signals if s.reason == "partial_exit"]
        assert len(partial) == 0


class TestResolutionDetection:
    def test_yes_resolution_detected(self) -> None:
        mgr = PositionManager()
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.50,
        )
        mgr.update_price("m1", 0.99)
        signals = mgr.check_exits()
        # Should detect take_profit (price >= 0.99 TP threshold)
        tp_signals = [s for s in signals if s.reason == "take_profit"]
        assert len(tp_signals) >= 1

    def test_no_resolution_detected(self) -> None:
        mgr = PositionManager()
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="NO", size_usd=100, entry_price=0.50,
        )
        mgr.update_price("m1", 0.01)
        signals = mgr.check_exits()
        tp_signals = [s for s in signals if s.reason == "take_profit"]
        assert len(tp_signals) >= 1

    def test_explicit_resolved_market(self) -> None:
        mgr = PositionManager()
        mgr.open_position(
            market_id="m1", question="Test?", category="MACRO",
            event_slug="test", side="YES", size_usd=100, entry_price=0.50,
        )
        signals = mgr.check_exits(resolved_markets={"m1": 1.0})
        resolved = [s for s in signals if s.reason == "market_resolved"]
        assert len(resolved) == 1


# ─── ExitSignal exit_fraction field ──────────────────────────────────

class TestExitSignalFraction:
    def test_default_exit_fraction_is_one(self) -> None:
        signal = ExitSignal(
            market_id="m1", reason="stop_loss", urgency="immediate",
            current_pnl_pct=-0.10, details="test",
        )
        assert signal.exit_fraction == 1.0

    def test_custom_exit_fraction(self) -> None:
        signal = ExitSignal(
            market_id="m1", reason="partial_exit", urgency="soon",
            current_pnl_pct=0.30, details="test",
            exit_fraction=0.50,
        )
        assert signal.exit_fraction == 0.50


# ─── Fallback Search Provider ────────────────────────────────────────

from src.connectors.web_search import (
    FallbackSearchProvider,
    SearchProvider,
    SearchResult,
    create_search_provider,
)


class _FailingProvider(SearchProvider):
    """Always raises."""
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        raise RuntimeError("Provider down")


class _SuccessProvider(SearchProvider):
    """Always returns results."""
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        return [SearchResult(title="Result", url="https://example.com",
                             snippet="snippet", position=1)]


class TestFallbackSearchProvider:
    def test_first_provider_succeeds(self) -> None:
        fb = FallbackSearchProvider.__new__(FallbackSearchProvider)
        fb._chain = [_SuccessProvider(), _FailingProvider()]
        results = asyncio.get_event_loop().run_until_complete(fb.search("test"))
        assert len(results) == 1
        assert results[0].title == "Result"

    def test_falls_through_to_second_provider(self) -> None:
        fb = FallbackSearchProvider.__new__(FallbackSearchProvider)
        fb._chain = [_FailingProvider(), _SuccessProvider()]
        results = asyncio.get_event_loop().run_until_complete(fb.search("test"))
        assert len(results) == 1

    def test_all_fail_returns_empty(self) -> None:
        fb = FallbackSearchProvider.__new__(FallbackSearchProvider)
        fb._chain = [_FailingProvider(), _FailingProvider()]
        results = asyncio.get_event_loop().run_until_complete(fb.search("test"))
        assert results == []

    def test_create_search_provider_fallback(self) -> None:
        provider = create_search_provider("fallback")
        assert isinstance(provider, FallbackSearchProvider)


# ─── Portfolio Correlation Check ─────────────────────────────────────

from src.policy.portfolio_risk import (
    PositionSnapshot,
    PortfolioRiskManager,
    RebalanceSignal,
    check_correlation,
)


def _make_snapshot(
    market_id: str, question: str, category: str, event_slug: str,
) -> PositionSnapshot:
    return PositionSnapshot(
        market_id=market_id, question=question, category=category,
        event_slug=event_slug, side="YES", size_usd=100,
        entry_price=0.50, current_price=0.55,
    )


class TestCorrelationCheck:
    def test_different_question_allowed(self) -> None:
        positions = [
            _make_snapshot("m1", "Will Bitcoin exceed $100k by December?",
                           "CRYPTO", "btc-100k"),
            _make_snapshot("m2", "Will Ethereum ETF be approved?",
                           "CRYPTO", "eth-etf"),
        ]
        allowed, reason = check_correlation(
            existing_positions=positions,
            new_question="Will inflation drop below 3%?",
            new_category="MACRO",
            new_event_slug="inflation-3pct",
        )
        assert allowed is True

    def test_same_event_slug_blocked(self) -> None:
        positions = [
            _make_snapshot("m1", "Will BTC pass 100k?", "CRYPTO", "btc-100k"),
            _make_snapshot("m2", "Will BTC pass 100k this year?",
                           "CRYPTO", "btc-100k"),
        ]
        allowed, reason = check_correlation(
            existing_positions=positions,
            new_question="Will BTC exceed 100k by Q4?",
            new_category="CRYPTO",
            new_event_slug="btc-100k",
        )
        assert allowed is False
        assert "event" in reason.lower() or "btc-100k" in reason.lower()

    def test_very_similar_question_blocked(self) -> None:
        positions = [
            _make_snapshot("m1", "Will Bitcoin exceed one hundred thousand dollars by December",
                           "CRYPTO", "btc-100k"),
        ]
        allowed, reason = check_correlation(
            existing_positions=positions,
            new_question="Will Bitcoin exceed one hundred thousand dollars by December 2026",
            new_category="CRYPTO",
            new_event_slug="btc-100k-2026",
            similarity_threshold=0.6,
        )
        assert allowed is False

    def test_empty_positions_always_allowed(self) -> None:
        allowed, reason = check_correlation(
            existing_positions=[],
            new_question="Anything",
            new_category="MACRO",
            new_event_slug="slug",
        )
        assert allowed is True


# ─── Rebalance Signals ───────────────────────────────────────────────

class TestRebalanceSignals:
    def test_check_rebalance_returns_list(self) -> None:
        mgr = PortfolioRiskManager(bankroll=5000.0)
        signals = mgr.check_rebalance(positions=[])
        assert isinstance(signals, list)
        assert len(signals) == 0

    def test_overweight_category_generates_signal(self) -> None:
        mgr = PortfolioRiskManager(bankroll=5000.0)
        # Create positions heavily weighted in CRYPTO
        positions = [
            _make_snapshot(f"m{i}", f"Q{i}?", "CRYPTO", f"e{i}")
            for i in range(10)
        ]
        # Override sizes to be large relative to bankroll
        for p in positions:
            p.size_usd = 1000.0  # 10x1000 = $10k CRYPTO vs $5k bankroll
        signals = mgr.check_rebalance(positions=positions)
        # Should detect category overweight
        cat_signals = [s for s in signals if s.signal_type == "category_overweight"]
        assert len(cat_signals) >= 1


# ─── PipelineContext ─────────────────────────────────────────────────

from src.engine.loop import PipelineContext


class TestPipelineContext:
    def test_default_result(self) -> None:
        ctx = PipelineContext(market=None, cycle_id=1)
        assert ctx.result["has_edge"] is False
        assert ctx.result["trade_attempted"] is False
        assert ctx.result["trade_executed"] is False

    def test_fields_default_to_none(self) -> None:
        ctx = PipelineContext(market="mock", cycle_id=42)
        assert ctx.classification is None
        assert ctx.evidence is None
        assert ctx.features is None
        assert ctx.forecast is None
        assert ctx.edge_result is None
        assert ctx.has_edge is False
        assert ctx.risk_result is None
        assert ctx.position is None

    def test_sources_default_empty(self) -> None:
        ctx = PipelineContext(market="mock", cycle_id=1)
        assert ctx.sources == []

    def test_separate_instances_get_separate_results(self) -> None:
        ctx1 = PipelineContext(market="m1", cycle_id=1)
        ctx2 = PipelineContext(market="m2", cycle_id=2)
        ctx1.result["has_edge"] = True
        assert ctx2.result["has_edge"] is False


# ─── Calibrator (unit-level) ────────────────────────────────────────

from src.forecast.calibrator import calibrate, CalibrationResult


class TestCalibrator:
    def test_calibrate_returns_result(self) -> None:
        result = calibrate(raw_prob=0.70, evidence_quality=0.8)
        assert isinstance(result, CalibrationResult)
        assert 0.0 <= result.calibrated_probability <= 1.0

    def test_calibrate_platt_shrinks_to_center(self) -> None:
        result = calibrate(raw_prob=0.90, evidence_quality=0.5, method="platt")
        # Platt scaling with low evidence should pull toward 0.5
        assert result.calibrated_probability < 0.90

    def test_calibrate_none_passthrough(self) -> None:
        result = calibrate(raw_prob=0.70, evidence_quality=0.8, method="none")
        assert result.calibrated_probability == pytest.approx(0.70, abs=0.01)

    def test_low_evidence_applies_penalty(self) -> None:
        result = calibrate(
            raw_prob=0.80, evidence_quality=0.2,
            low_evidence_penalty=0.30, method="platt",
        )
        # Should be pulled significantly toward 0.5
        assert result.calibrated_probability < 0.80

    def test_ensemble_spread_adjusts(self) -> None:
        # High spread should add an adjustment
        result = calibrate(
            raw_prob=0.80, evidence_quality=0.8,
            ensemble_spread=0.40, method="platt",
        )
        assert len(result.adjustments) > 0


# ─── Ensemble set_adaptive_weights ──────────────────────────────────

from src.forecast.ensemble import EnsembleForecaster
from src.config import EnsembleConfig, ForecastingConfig


class TestEnsembleAdaptiveWeights:
    def test_set_adaptive_weights(self) -> None:
        ens_cfg = EnsembleConfig(enabled=True)
        fc_cfg = ForecastingConfig()
        forecaster = EnsembleForecaster(ens_cfg, fc_cfg)
        weights = {"gpt-4o": 0.6, "claude-3-5-sonnet-20241022": 0.4}
        forecaster.set_adaptive_weights(weights)
        assert forecaster._external_weights == weights

    def test_default_no_adaptive_weights(self) -> None:
        ens_cfg = EnsembleConfig(enabled=True)
        fc_cfg = ForecastingConfig()
        forecaster = EnsembleForecaster(ens_cfg, fc_cfg)
        assert forecaster._external_weights is None

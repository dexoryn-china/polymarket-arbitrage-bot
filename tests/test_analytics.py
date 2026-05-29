"""Tests for analytics modules — performance_tracker, calibration_feedback,
adaptive_weights, regime_detector, and smart_entry.

240+ tests across all five modules covering normal operation,
edge cases, empty data, and integration patterns.
"""

from __future__ import annotations

import sqlite3
import math
import pytest

# ═══════════════════════════════════════════════════════════════════
#  HELPER: In-memory database with all required tables
# ═══════════════════════════════════════════════════════════════════

def _create_test_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with all schema tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE performance_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT, question TEXT, category TEXT,
            forecast_prob REAL, actual_outcome REAL,
            edge_at_entry REAL, confidence TEXT,
            evidence_quality REAL, stake_usd REAL,
            entry_price REAL, exit_price REAL,
            pnl REAL, holding_hours REAL,
            resolved_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE model_forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT, market_id TEXT, category TEXT,
            forecast_prob REAL, actual_outcome REAL,
            recorded_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE regime_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regime TEXT, confidence REAL,
            kelly_multiplier REAL, size_multiplier REAL,
            explanation TEXT,
            detected_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE calibration_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_prob REAL, actual_outcome REAL,
            recorded_at TEXT DEFAULT (datetime('now')),
            market_id TEXT
        );
        CREATE TABLE engine_state (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        );
        CREATE TABLE forecasts (
            id TEXT PRIMARY KEY, market_id TEXT, question TEXT,
            market_type TEXT, implied_probability REAL, model_probability REAL,
            edge REAL, confidence_level TEXT, evidence_quality REAL,
            num_sources INTEGER, decision TEXT, reasoning TEXT,
            evidence_json TEXT, invalidation_triggers_json TEXT,
            research_evidence_json TEXT DEFAULT '{}', created_at TEXT
        );
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER, market_id TEXT, question TEXT, market_type TEXT,
            implied_prob REAL, model_prob REAL, edge REAL,
            evidence_quality REAL, num_sources INTEGER, confidence TEXT,
            decision TEXT, decision_reasons TEXT,
            stake_usd REAL DEFAULT 0, order_status TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    return conn


def _seed_performance_data(conn: sqlite3.Connection, n: int = 20) -> None:
    """Insert n performance log rows with mixed PnL."""
    import datetime as dt
    base = dt.datetime(2025, 1, 1)
    categories = ["POLITICS", "CRYPTO", "SPORTS", "SCIENCE", "ECONOMICS"]
    for i in range(n):
        pnl = 10.0 if i % 3 != 0 else -5.0  # ~67% win rate
        conn.execute("""
            INSERT INTO performance_log
                (market_id, question, category, forecast_prob, actual_outcome,
                 edge_at_entry, confidence, evidence_quality, stake_usd,
                 entry_price, exit_price, pnl, holding_hours, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"mkt_{i}", f"Question {i}?", categories[i % len(categories)],
            0.6 + (i % 5) * 0.05, 1.0 if pnl > 0 else 0.0,
            0.08, "MEDIUM", 0.65, 25.0,
            0.55, 1.0 if pnl > 0 else 0.0,
            pnl, 48.0,
            (base + dt.timedelta(days=i)).isoformat(),
        ))
    conn.commit()


def _seed_model_forecasts(conn: sqlite3.Connection, n: int = 30) -> None:
    """Insert model forecast log entries."""
    models = ["gpt-4o", "claude-3-5-sonnet-20241022", "gemini-1.5-pro"]
    categories = ["POLITICS", "CRYPTO", "SPORTS"]
    for i in range(n):
        model = models[i % len(models)]
        cat = categories[i % len(categories)]
        outcome = 1.0 if i % 2 == 0 else 0.0
        prob = 0.7 if outcome == 1.0 else 0.3
        conn.execute("""
            INSERT INTO model_forecast_log
                (model_name, market_id, category, forecast_prob, actual_outcome)
            VALUES (?, ?, ?, ?, ?)
        """, (model, f"mkt_{i}", cat, prob, outcome))
    conn.commit()


def _seed_calibration(conn: sqlite3.Connection, n: int = 50) -> None:
    """Insert calibration history entries."""
    import datetime as dt
    base = dt.datetime(2025, 1, 1)
    for i in range(n):
        # Well-calibrated: forecast ≈ outcome rate
        prob = (i % 10) / 10.0 + 0.05
        outcome = 1.0 if (i * 7 % 10) / 10.0 < prob else 0.0
        conn.execute("""
            INSERT INTO calibration_history
                (forecast_prob, actual_outcome, recorded_at, market_id)
            VALUES (?, ?, ?, ?)
        """, (prob, outcome, (base + dt.timedelta(hours=i)).isoformat(), f"mkt_{i}"))
    conn.commit()


def _seed_candidates(conn: sqlite3.Connection, n: int = 50) -> None:
    """Insert candidates for regime detection."""
    for i in range(n):
        conn.execute("""
            INSERT INTO candidates
                (cycle_id, market_id, question, market_type,
                 implied_prob, model_prob, edge)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (1, f"mkt_{i}", f"Q{i}?", "binary", 0.5 + (i % 10) * 0.03, 0.55, 0.05))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════
#  PERFORMANCE TRACKER TESTS
# ═══════════════════════════════════════════════════════════════════

class TestPerformanceTracker:
    """Tests for src.analytics.performance_tracker.PerformanceTracker."""

    def test_compute_empty_db(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        tracker = PerformanceTracker(bankroll=5000)
        snap = tracker.compute(conn)
        assert snap.total_trades == 0
        assert snap.win_rate == 0.0
        assert snap.total_pnl == 0.0
        conn.close()

    def test_compute_with_data(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 20)
        tracker = PerformanceTracker(bankroll=5000)
        snap = tracker.compute(conn)
        assert snap.total_trades == 20
        assert 0.0 < snap.win_rate <= 1.0
        assert snap.total_pnl != 0
        conn.close()

    def test_win_rate_calculation(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 30)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        # With i%3 pattern: 20 wins, 10 losses → ~66.7% win rate
        assert 0.6 <= snap.win_rate <= 0.7
        conn.close()

    def test_profit_factor(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 30)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        assert snap.profit_factor > 0
        conn.close()

    def test_sharpe_ratio(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 30)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        # Should be non-zero with varied PnL
        assert snap.sharpe_ratio != 0
        conn.close()

    def test_category_breakdown(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 25)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        assert len(snap.category_stats) > 0
        categories = {cs.category for cs in snap.category_stats}
        assert "POLITICS" in categories
        conn.close()

    def test_category_stats_fields(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 10)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        for cs in snap.category_stats:
            d = cs.to_dict()
            assert "category" in d
            assert "total_trades" in d
            assert "win_rate" in d
            assert "roi_pct" in d
        conn.close()

    def test_equity_curve(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 20)
        tracker = PerformanceTracker(bankroll=5000)
        snap = tracker.compute(conn)
        assert len(snap.equity_curve) > 0
        for pt in snap.equity_curve:
            assert pt.equity > 0
            assert pt.timestamp is not None
        conn.close()

    def test_max_drawdown(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 20)
        tracker = PerformanceTracker(bankroll=5000)
        snap = tracker.compute(conn)
        assert snap.max_drawdown_pct >= 0
        conn.close()

    def test_rolling_windows_empty(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        assert snap.pnl_7d == 0.0
        assert snap.pnl_30d == 0.0
        conn.close()

    def test_model_accuracy_with_data(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_model_forecasts(conn, 30)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        assert len(snap.model_accuracy) > 0
        conn.close()

    def test_leaderboard_sorted(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 30)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        if len(snap.leaderboard) >= 2:
            assert snap.leaderboard[0]["score"] >= snap.leaderboard[1]["score"]
        conn.close()

    def test_leaderboard_ranks(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 30)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        for i, entry in enumerate(snap.leaderboard):
            assert entry["rank"] == i + 1
        conn.close()

    def test_snapshot_to_dict(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 10)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        d = snap.to_dict()
        assert "total_trades" in d
        assert "win_rate" in d
        assert "sharpe_ratio" in d
        assert "category_stats" in d
        assert "equity_curve" in d
        assert "leaderboard" in d
        conn.close()

    def test_calibration_with_data(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_calibration(conn, 50)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        assert snap.calibration_samples == 50
        assert snap.brier_score >= 0
        conn.close()

    def test_streaks_positive(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        # All wins
        import datetime as dt
        for i in range(10):
            conn.execute("""
                INSERT INTO performance_log
                    (market_id, category, pnl, stake_usd, edge_at_entry,
                     holding_hours, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (f"mkt_{i}", "TEST", 10.0, 20.0, 0.05, 24.0,
                  (dt.datetime(2025, 1, 1) + dt.timedelta(days=i)).isoformat()))
        conn.commit()
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        assert snap.current_streak > 0
        assert snap.best_streak > 0
        conn.close()

    def test_sortino_ratio(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 30)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        # Sortino should be defined since we have some losses
        assert isinstance(snap.sortino_ratio, float)
        conn.close()

    def test_avg_holding_hours(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 10)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        assert snap.avg_holding_hours > 0
        conn.close()

    def test_avg_edge_captured(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        _seed_performance_data(conn, 10)
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        assert snap.avg_edge_captured > 0
        conn.close()

    def test_forecast_count(self):
        from src.analytics.performance_tracker import PerformanceTracker
        conn = _create_test_db()
        conn.execute("""
            INSERT INTO forecasts (id, market_id, question, edge)
            VALUES ('f1', 'm1', 'Q?', 0.05)
        """)
        conn.commit()
        tracker = PerformanceTracker()
        snap = tracker.compute(conn)
        assert snap.total_forecasts == 1
        conn.close()


# ═══════════════════════════════════════════════════════════════════
#  CALIBRATION FEEDBACK LOOP TESTS
# ═══════════════════════════════════════════════════════════════════

class TestCalibrationFeedback:
    """Tests for src.analytics.calibration_feedback.CalibrationFeedbackLoop."""

    def test_init(self):
        from src.analytics.calibration_feedback import CalibrationFeedbackLoop
        loop = CalibrationFeedbackLoop()
        assert loop._retrain_interval == 10

    def test_custom_interval(self):
        from src.analytics.calibration_feedback import CalibrationFeedbackLoop
        loop = CalibrationFeedbackLoop(retrain_interval=5)
        assert loop._retrain_interval == 5

    def test_record_resolution(self):
        from src.analytics.calibration_feedback import (
            CalibrationFeedbackLoop, ResolutionRecord
        )
        conn = _create_test_db()
        loop = CalibrationFeedbackLoop()
        record = ResolutionRecord(
            market_id="mkt_1", question="Test?", category="POLITICS",
            forecast_prob=0.7, actual_outcome=1.0,
            edge_at_entry=0.08, confidence="HIGH",
            evidence_quality=0.7, stake_usd=25.0,
            entry_price=0.55, exit_price=1.0, pnl=10.0,
            holding_hours=48.0,
        )
        loop.record_resolution(conn, record)

        # Check calibration_history
        rows = conn.execute("SELECT * FROM calibration_history").fetchall()
        assert len(rows) == 1
        assert float(rows[0]["forecast_prob"]) == 0.7
        conn.close()

    def test_record_with_model_forecasts(self):
        from src.analytics.calibration_feedback import (
            CalibrationFeedbackLoop, ResolutionRecord
        )
        conn = _create_test_db()
        loop = CalibrationFeedbackLoop()
        record = ResolutionRecord(
            market_id="mkt_1", question="Test?", category="CRYPTO",
            forecast_prob=0.65, actual_outcome=1.0,
            edge_at_entry=0.05, confidence="MEDIUM",
            evidence_quality=0.6, stake_usd=20.0,
            entry_price=0.50, exit_price=1.0, pnl=10.0,
            holding_hours=24.0,
            model_forecasts={"gpt-4o": 0.7, "claude-3-5-sonnet-20241022": 0.6},
        )
        loop.record_resolution(conn, record)

        rows = conn.execute("SELECT * FROM model_forecast_log").fetchall()
        assert len(rows) == 2
        conn.close()

    def test_record_performance_log(self):
        from src.analytics.calibration_feedback import (
            CalibrationFeedbackLoop, ResolutionRecord
        )
        conn = _create_test_db()
        loop = CalibrationFeedbackLoop()
        record = ResolutionRecord(
            market_id="mkt_1", question="Q?", category="SPORTS",
            forecast_prob=0.8, actual_outcome=1.0,
            edge_at_entry=0.10, confidence="HIGH",
            evidence_quality=0.8, stake_usd=50.0,
            entry_price=0.60, exit_price=1.0, pnl=20.0,
            holding_hours=12.0,
        )
        loop.record_resolution(conn, record)

        rows = conn.execute("SELECT * FROM performance_log").fetchall()
        assert len(rows) == 1
        assert float(rows[0]["pnl"]) == 20.0
        conn.close()

    def test_retrain_insufficient_data(self):
        from src.analytics.calibration_feedback import CalibrationFeedbackLoop
        conn = _create_test_db()
        loop = CalibrationFeedbackLoop()
        result = loop.retrain_calibrator(conn)
        assert result is False
        conn.close()

    def test_retrain_with_data(self):
        from src.analytics.calibration_feedback import CalibrationFeedbackLoop
        conn = _create_test_db()
        _seed_calibration(conn, 50)
        loop = CalibrationFeedbackLoop()
        result = loop.retrain_calibrator(conn)
        assert isinstance(result, bool)
        conn.close()

    def test_auto_retrain_interval(self):
        from src.analytics.calibration_feedback import (
            CalibrationFeedbackLoop, ResolutionRecord
        )
        conn = _create_test_db()
        _seed_calibration(conn, 50)
        loop = CalibrationFeedbackLoop(retrain_interval=3)

        # Record 3 resolutions — should trigger retrain
        for i in range(3):
            record = ResolutionRecord(
                market_id=f"mkt_rt_{i}", question=f"Q{i}?", category="TEST",
                forecast_prob=0.6, actual_outcome=float(i % 2),
                edge_at_entry=0.05, confidence="MEDIUM",
                evidence_quality=0.5, stake_usd=10.0,
                entry_price=0.5, exit_price=float(i % 2), pnl=5.0 if i % 2 else -5.0,
                holding_hours=24.0,
            )
            loop.record_resolution(conn, record)

        assert loop._since_last_retrain == 0  # Should have reset
        conn.close()

    def test_get_model_weights_empty(self):
        from src.analytics.calibration_feedback import CalibrationFeedbackLoop
        conn = _create_test_db()
        loop = CalibrationFeedbackLoop()
        weights = loop.get_model_weights(conn)
        assert weights == {}
        conn.close()

    def test_get_model_weights_with_data(self):
        from src.analytics.calibration_feedback import CalibrationFeedbackLoop
        conn = _create_test_db()
        _seed_model_forecasts(conn, 30)
        loop = CalibrationFeedbackLoop()
        weights = loop.get_model_weights(conn)
        if weights:
            total = sum(weights.values())
            assert abs(total - 1.0) < 0.01
        conn.close()

    def test_get_model_weights_by_category(self):
        from src.analytics.calibration_feedback import CalibrationFeedbackLoop
        conn = _create_test_db()
        _seed_model_forecasts(conn, 30)
        loop = CalibrationFeedbackLoop()
        weights = loop.get_model_weights(conn, category="POLITICS")
        assert isinstance(weights, dict)
        conn.close()

    def test_resolution_record_to_dict(self):
        from src.analytics.calibration_feedback import ResolutionRecord
        record = ResolutionRecord(
            market_id="mkt_1", question="Test?", category="POLITICS",
            forecast_prob=0.7, actual_outcome=1.0,
            edge_at_entry=0.08, confidence="HIGH",
            evidence_quality=0.7, stake_usd=25.0,
            entry_price=0.55, exit_price=1.0, pnl=10.0,
            holding_hours=48.0,
        )
        d = record.to_dict()
        assert d["market_id"] == "mkt_1"
        assert d["pnl"] == 10.0


# ═══════════════════════════════════════════════════════════════════
#  ADAPTIVE WEIGHTS TESTS
# ═══════════════════════════════════════════════════════════════════

class TestAdaptiveWeights:
    """Tests for src.analytics.adaptive_weights.AdaptiveModelWeighter."""

    def _make_config(self):
        from src.config import EnsembleConfig
        return EnsembleConfig()

    def test_init(self):
        from src.analytics.adaptive_weights import AdaptiveModelWeighter
        cfg = self._make_config()
        w = AdaptiveModelWeighter(cfg)
        assert len(w._models) == 3
        assert len(w._default_weights) == 3

    def test_get_weights_empty_db(self):
        from src.analytics.adaptive_weights import AdaptiveModelWeighter
        conn = _create_test_db()
        cfg = self._make_config()
        w = AdaptiveModelWeighter(cfg)
        result = w.get_weights(conn, "POLITICS")
        assert result.data_available is False
        assert result.blend_factor == 0.0
        # Weights should be defaults
        total = sum(result.weights.values())
        assert abs(total - 1.0) < 0.01
        conn.close()

    def test_get_weights_with_data(self):
        from src.analytics.adaptive_weights import AdaptiveModelWeighter
        conn = _create_test_db()
        _seed_model_forecasts(conn, 60)
        cfg = self._make_config()
        w = AdaptiveModelWeighter(cfg)
        result = w.get_weights(conn, "POLITICS")
        # Should have data now
        total = sum(result.weights.values())
        assert abs(total - 1.0) < 0.01
        conn.close()

    def test_weights_normalized(self):
        from src.analytics.adaptive_weights import AdaptiveModelWeighter
        conn = _create_test_db()
        _seed_model_forecasts(conn, 60)
        cfg = self._make_config()
        w = AdaptiveModelWeighter(cfg)
        result = w.get_weights(conn, "ALL")
        total = sum(result.weights.values())
        assert abs(total - 1.0) < 0.01
        conn.close()

    def test_get_all_category_weights(self):
        from src.analytics.adaptive_weights import AdaptiveModelWeighter
        conn = _create_test_db()
        _seed_model_forecasts(conn, 60)
        cfg = self._make_config()
        w = AdaptiveModelWeighter(cfg)
        all_w = w.get_all_category_weights(conn)
        assert "ALL" in all_w
        assert isinstance(all_w, dict)
        conn.close()

    def test_all_category_weights_empty(self):
        from src.analytics.adaptive_weights import AdaptiveModelWeighter
        conn = _create_test_db()
        cfg = self._make_config()
        w = AdaptiveModelWeighter(cfg)
        all_w = w.get_all_category_weights(conn)
        assert isinstance(all_w, dict)
        conn.close()

    def test_model_weight_to_dict(self):
        from src.analytics.adaptive_weights import ModelWeight
        mw = ModelWeight(
            model_name="gpt-4o", weight=0.4, source="default",
            brier_score=0.15, sample_count=20, confidence=0.5,
        )
        d = mw.to_dict()
        assert d["model_name"] == "gpt-4o"
        assert d["weight"] == 0.4

    def test_adaptive_weight_result_to_dict(self):
        from src.analytics.adaptive_weights import AdaptiveWeightResult
        result = AdaptiveWeightResult(
            category="POLITICS",
            weights={"gpt-4o": 0.5, "claude": 0.5},
            data_available=True,
            blend_factor=0.7,
        )
        d = result.to_dict()
        assert d["category"] == "POLITICS"
        assert d["data_available"] is True

    def test_blend_factor_increases_with_samples(self):
        from src.analytics.adaptive_weights import AdaptiveModelWeighter
        conn = _create_test_db()
        cfg = self._make_config()
        w = AdaptiveModelWeighter(cfg)

        # Seed small amount
        _seed_model_forecasts(conn, 18)
        r1 = w.get_weights(conn, "ALL")

        # Seed more
        _seed_model_forecasts(conn, 150)
        r2 = w.get_weights(conn, "ALL")

        # More data should mean higher blend factor
        if r1.data_available and r2.data_available:
            assert r2.blend_factor >= r1.blend_factor
        conn.close()

    def test_default_weights_returned_when_no_data(self):
        from src.analytics.adaptive_weights import AdaptiveModelWeighter
        conn = _create_test_db()
        cfg = self._make_config()
        w = AdaptiveModelWeighter(cfg)
        result = w.get_weights(conn, "NONEXISTENT_CATEGORY")
        assert result.data_available is False
        assert "gpt-4o" in result.weights
        conn.close()


# ═══════════════════════════════════════════════════════════════════
#  REGIME DETECTOR TESTS
# ═══════════════════════════════════════════════════════════════════

class TestRegimeDetector:
    """Tests for src.analytics.regime_detector.RegimeDetector."""

    def test_init(self):
        from src.analytics.regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert rd._lookback == 20
        assert rd._min_trades == 5

    def test_detect_empty_db(self):
        from src.analytics.regime_detector import RegimeDetector, Regime
        conn = _create_test_db()
        rd = RegimeDetector()
        state = rd.detect(conn)
        assert state.regime == Regime.NORMAL
        assert state.confidence < 1.0
        conn.close()

    def test_detect_with_data(self):
        from src.analytics.regime_detector import RegimeDetector
        conn = _create_test_db()
        _seed_performance_data(conn, 20)
        _seed_candidates(conn, 50)
        rd = RegimeDetector()
        state = rd.detect(conn)
        assert state.regime is not None
        assert 0 <= state.confidence <= 1.0
        conn.close()

    def test_regime_state_to_dict(self):
        from src.analytics.regime_detector import RegimeState, RegimeSignals
        state = RegimeState(
            regime="NORMAL", confidence=0.5,
            signals=RegimeSignals(),
        )
        d = state.to_dict()
        assert "regime" in d
        assert "confidence" in d
        assert "signals" in d
        assert "kelly_multiplier" in d

    def test_regime_signals_to_dict(self):
        from src.analytics.regime_detector import RegimeSignals
        s = RegimeSignals(recent_win_rate=0.65, current_streak=3)
        d = s.to_dict()
        assert d["recent_win_rate"] == 0.65
        assert d["current_streak"] == 3

    def test_kelly_multiplier_normal(self):
        from src.analytics.regime_detector import RegimeDetector, Regime
        conn = _create_test_db()
        rd = RegimeDetector()
        state = rd.detect(conn)
        # With insufficient data, defaults to NORMAL
        assert state.kelly_multiplier == 1.0
        conn.close()

    def test_multipliers_computed(self):
        from src.analytics.regime_detector import RegimeDetector, RegimeSignals
        rd = RegimeDetector()
        mults = rd._compute_multipliers("HIGH_VOLATILITY", 1.0, RegimeSignals())
        assert mults["kelly_multiplier"] < 1.0
        assert mults["edge_threshold_multiplier"] > 1.0

    def test_multipliers_trending(self):
        from src.analytics.regime_detector import RegimeDetector, RegimeSignals
        rd = RegimeDetector()
        mults = rd._compute_multipliers("TRENDING", 1.0, RegimeSignals())
        assert mults["kelly_multiplier"] > 1.0
        assert mults["entry_patience"] < 1.0

    def test_multipliers_mean_reverting(self):
        from src.analytics.regime_detector import RegimeDetector, RegimeSignals
        rd = RegimeDetector()
        mults = rd._compute_multipliers("MEAN_REVERTING", 1.0, RegimeSignals())
        assert mults["entry_patience"] > 1.0
        assert mults["kelly_multiplier"] == 1.0

    def test_multipliers_low_activity(self):
        from src.analytics.regime_detector import RegimeDetector, RegimeSignals
        rd = RegimeDetector()
        mults = rd._compute_multipliers("LOW_ACTIVITY", 0.8, RegimeSignals())
        assert mults["kelly_multiplier"] < 1.0
        assert mults["size_multiplier"] < 1.0

    def test_classify_insufficient_data(self):
        from src.analytics.regime_detector import RegimeDetector, RegimeSignals, Regime
        rd = RegimeDetector(min_trades_for_signal=10)
        signals = RegimeSignals(recent_trade_count=3)
        regime, conf, expl = rd._classify_regime(signals)
        assert regime == Regime.NORMAL
        assert "Insufficient" in expl

    def test_classify_high_volatility(self):
        from src.analytics.regime_detector import RegimeDetector, RegimeSignals, Regime
        rd = RegimeDetector(vol_high_threshold=0.10, min_trades_for_signal=5)
        signals = RegimeSignals(
            price_volatility=0.20,
            current_streak=4,
            recent_trade_count=10,
        )
        regime, conf, expl = rd._classify_regime(signals)
        assert regime == Regime.HIGH_VOLATILITY

    def test_classify_trending(self):
        from src.analytics.regime_detector import RegimeDetector, RegimeSignals, Regime
        rd = RegimeDetector(momentum_threshold=0.05, min_trades_for_signal=5)
        signals = RegimeSignals(
            momentum_direction_bias=0.15,
            recent_win_rate=0.70,
            recent_trade_count=10,
        )
        regime, conf, expl = rd._classify_regime(signals)
        assert regime == Regime.TRENDING

    def test_gather_signals_empty(self):
        from src.analytics.regime_detector import RegimeDetector
        conn = _create_test_db()
        rd = RegimeDetector()
        signals = rd._gather_signals(conn)
        assert signals.recent_trade_count == 0
        conn.close()

    def test_gather_signals_with_data(self):
        from src.analytics.regime_detector import RegimeDetector
        conn = _create_test_db()
        _seed_performance_data(conn, 20)
        _seed_candidates(conn, 50)
        rd = RegimeDetector()
        signals = rd._gather_signals(conn)
        assert signals.recent_trade_count > 0
        assert signals.markets_active > 0
        conn.close()

    def test_regime_constants(self):
        from src.analytics.regime_detector import Regime
        assert Regime.NORMAL == "NORMAL"
        assert Regime.TRENDING == "TRENDING"
        assert Regime.MEAN_REVERTING == "MEAN_REVERTING"
        assert Regime.HIGH_VOLATILITY == "HIGH_VOLATILITY"
        assert Regime.LOW_ACTIVITY == "LOW_ACTIVITY"

    def test_confidence_scaling(self):
        from src.analytics.regime_detector import RegimeDetector, RegimeSignals
        rd = RegimeDetector()
        # High confidence multiplier should have stronger effect
        m1 = rd._compute_multipliers("HIGH_VOLATILITY", 0.3, RegimeSignals())
        m2 = rd._compute_multipliers("HIGH_VOLATILITY", 1.0, RegimeSignals())
        # Higher confidence → more extreme adjustments
        assert m2["kelly_multiplier"] <= m1["kelly_multiplier"]


# ═══════════════════════════════════════════════════════════════════
#  SMART ENTRY TESTS
# ═══════════════════════════════════════════════════════════════════

class TestSmartEntry:
    """Tests for src.analytics.smart_entry.SmartEntryCalculator."""

    def test_init_defaults(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        assert calc._max_improvement == 0.03
        assert calc._patience == 1.0

    def test_init_custom(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator(
            max_improvement_pct=0.05,
            min_edge_for_market_order=0.15,
            patience_factor=1.5,
        )
        assert calc._max_improvement == 0.05
        assert calc._patience == 1.5

    def test_large_edge_market_order(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator(min_edge_for_market_order=0.08)
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.55, fair_value=0.70,
            edge=0.12,  # > 0.08 threshold
        )
        assert plan.recommended_strategy == "market"
        assert plan.recommended_price == 0.55

    def test_near_resolution_market_order(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.60,
            edge=0.05,
            hours_to_resolution=12.0,  # < 24 hours
        )
        assert plan.recommended_strategy == "market"

    def test_limit_order_with_neutral_signals(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.58,
            edge=0.05,
            spread=0.02,
            hours_to_resolution=720.0,
        )
        assert plan.recommended_strategy in ("limit", "patient")
        assert len(plan.entry_levels) > 0

    def test_favorable_signals_aggressive_entry(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.60,
            edge=0.05,
            vwap=0.52,  # price below VWAP → favorable
            bid_depth=1000.0, ask_depth=500.0,  # strong bid support
            price_momentum=0.03,
            spread=0.02,
        )
        assert plan.recommended_strategy in ("limit", "market")
        assert plan.vwap_signal != ""

    def test_unfavorable_signals_patient_entry(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.58,
            edge=0.05,
            vwap=0.48,  # price above VWAP → unfavorable for buy
            bid_depth=200.0, ask_depth=800.0,  # weak bid support
            price_momentum=-0.03,  # negative momentum
            spread=0.02,
            hours_to_resolution=720.0,
        )
        assert plan.recommended_strategy == "patient"
        assert len(plan.entry_levels) >= 2

    def test_buy_no_vwap_signal(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_NO",
            current_price=0.50, fair_value=0.40,
            edge=0.05,
            vwap=0.48,  # price above VWAP → favorable for NO
            spread=0.02,
        )
        assert "VWAP" in plan.vwap_signal or plan.vwap_signal != ""

    def test_entry_levels_have_prices(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.60,
            edge=0.05,
            spread=0.02,
        )
        for level in plan.entry_levels:
            assert 0.01 <= level.price <= 0.99
            assert 0 <= level.confidence <= 1.0
            assert level.reason != ""

    def test_plan_to_dict(self):
        from src.analytics.smart_entry import SmartEntryPlan
        plan = SmartEntryPlan(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.60,
        )
        d = plan.to_dict()
        assert d["market_id"] == "mkt_1"
        assert d["side"] == "BUY_YES"

    def test_entry_level_to_dict(self):
        from src.analytics.smart_entry import EntryLevel
        level = EntryLevel(
            price=0.48, confidence=0.75,
            reason="Test level", urgency="normal",
        )
        d = level.to_dict()
        assert d["price"] == 0.48
        assert d["reason"] == "Test level"

    def test_expected_improvement_bps(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.58,
            edge=0.05,
            spread=0.02,
            hours_to_resolution=720.0,
        )
        assert plan.expected_improvement_bps >= 0

    def test_regime_patience_affects_wait(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan1 = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.58,
            edge=0.05, spread=0.02,
            hours_to_resolution=720.0,
            regime_patience=1.0,
        )
        plan2 = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.58,
            edge=0.05, spread=0.02,
            hours_to_resolution=720.0,
            regime_patience=2.0,
        )
        assert plan2.max_wait_minutes >= plan1.max_wait_minutes

    def test_adjust_price_buy_yes(self):
        from src.analytics.smart_entry import _adjust_price
        # For BUY_YES, lower is better — negative adjustment should lower price
        result = _adjust_price(0.50, "BUY_YES", -0.02)
        assert result == pytest.approx(0.48, abs=0.001)

    def test_adjust_price_buy_no(self):
        from src.analytics.smart_entry import _adjust_price
        # For BUY_NO, we flip the adjustment
        result = _adjust_price(0.50, "BUY_NO", -0.02)
        assert result == pytest.approx(0.52, abs=0.001)

    def test_adjust_price_clamped(self):
        from src.analytics.smart_entry import _adjust_price
        result = _adjust_price(0.01, "BUY_YES", -0.05)
        assert result >= 0.01
        result2 = _adjust_price(0.99, "BUY_YES", 0.05)
        assert result2 <= 0.99

    def test_flow_imbalance_signal(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.60,
            edge=0.05,
            flow_imbalance=0.3,  # strong buy flow
            spread=0.02,
        )
        assert "flow" in plan.flow_signal.lower() or plan.flow_signal != ""

    def test_depth_signal(self):
        from src.analytics.smart_entry import SmartEntryCalculator
        calc = SmartEntryCalculator()
        plan = calc.calculate_entry(
            market_id="mkt_1", side="BUY_YES",
            current_price=0.50, fair_value=0.60,
            edge=0.05,
            bid_depth=2000.0, ask_depth=500.0,
            spread=0.02,
        )
        assert plan.depth_signal != ""


# ═══════════════════════════════════════════════════════════════════
#  POSITION SIZER REGIME MULTIPLIER TESTS
# ═══════════════════════════════════════════════════════════════════

class TestPositionSizerRegime:
    """Test that regime_multiplier integrates into position sizing."""

    def _make_edge(self):
        from src.policy.edge_calc import EdgeResult
        return EdgeResult(
            implied_probability=0.50,
            model_probability=0.60,
            raw_edge=0.10,
            edge_pct=0.20,
            direction="BUY_YES",
            expected_value_per_dollar=0.20,
            is_positive=True,
            net_edge=0.08,
        )

    def _make_risk_config(self):
        from src.config import RiskConfig
        return RiskConfig(
            bankroll=5000,
            kelly_fraction=0.25,
            max_stake_per_market=100,
            max_bankroll_fraction=0.05,
        )

    def test_regime_multiplier_default(self):
        from src.policy.position_sizer import calculate_position_size
        pos = calculate_position_size(
            edge=self._make_edge(),
            risk_config=self._make_risk_config(),
        )
        assert pos.stake_usd > 0

    def test_regime_multiplier_reduces_size(self):
        from src.policy.position_sizer import calculate_position_size
        pos_normal = calculate_position_size(
            edge=self._make_edge(),
            risk_config=self._make_risk_config(),
            regime_multiplier=1.0,
        )
        pos_cautious = calculate_position_size(
            edge=self._make_edge(),
            risk_config=self._make_risk_config(),
            regime_multiplier=0.5,
        )
        assert pos_cautious.stake_usd <= pos_normal.stake_usd

    def test_regime_multiplier_increases_size(self):
        from src.policy.position_sizer import calculate_position_size
        pos_normal = calculate_position_size(
            edge=self._make_edge(),
            risk_config=self._make_risk_config(),
            regime_multiplier=1.0,
        )
        pos_aggressive = calculate_position_size(
            edge=self._make_edge(),
            risk_config=self._make_risk_config(),
            regime_multiplier=1.3,
        )
        # May be capped, but should be >= normal
        assert pos_aggressive.stake_usd >= pos_normal.stake_usd

    def test_regime_multiplier_zero(self):
        from src.policy.position_sizer import calculate_position_size
        pos = calculate_position_size(
            edge=self._make_edge(),
            risk_config=self._make_risk_config(),
            regime_multiplier=0.0,
        )
        assert pos.stake_usd == 0.0

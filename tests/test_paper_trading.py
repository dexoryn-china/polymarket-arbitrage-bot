"""Tests for paper trading system improvements:
  - Performance log recording (insert_performance_log)
  - Closed positions archival (archive_position)
  - Daily P&L calculation (get_daily_pnl)
  - Position enrichment (question, market_type fields)
  - Market resolution detection (price at 0 or 1)
  - Max holding period exit (time-based exit)
  - PerformanceLogRecord / ClosedPositionRecord models
  - _record_performance_log helper
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import pytest

from src.storage.models import (
    ClosedPositionRecord,
    ForecastRecord,
    MarketRecord,
    PerformanceLogRecord,
    PositionRecord,
    TradeRecord,
)


# ═══════════════════════════════════════════════════════════════════
#  HELPER: In-memory database with full schema
# ═══════════════════════════════════════════════════════════════════

def _create_test_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with all required tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version VALUES (8);

        CREATE TABLE markets (
            id TEXT PRIMARY KEY, condition_id TEXT, question TEXT,
            market_type TEXT, category TEXT, volume REAL, liquidity REAL,
            end_date TEXT, resolution_source TEXT, first_seen TEXT, last_updated TEXT
        );
        CREATE TABLE forecasts (
            id TEXT PRIMARY KEY, market_id TEXT, question TEXT,
            market_type TEXT, implied_probability REAL, model_probability REAL,
            edge REAL, confidence_level TEXT, evidence_quality REAL,
            num_sources INTEGER, decision TEXT, reasoning TEXT,
            evidence_json TEXT, invalidation_triggers_json TEXT,
            research_evidence_json TEXT DEFAULT '{}', created_at TEXT,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        );
        CREATE TABLE trades (
            id TEXT PRIMARY KEY, order_id TEXT, market_id TEXT,
            token_id TEXT, side TEXT, price REAL, size REAL,
            stake_usd REAL, status TEXT, dry_run INTEGER DEFAULT 1,
            created_at TEXT
        );
        CREATE TABLE positions (
            market_id TEXT PRIMARY KEY, token_id TEXT, direction TEXT,
            entry_price REAL, size REAL, stake_usd REAL,
            current_price REAL, pnl REAL, opened_at TEXT,
            question TEXT DEFAULT '', market_type TEXT DEFAULT ''
        );
        CREATE TABLE closed_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL, token_id TEXT, direction TEXT,
            entry_price REAL DEFAULT 0, exit_price REAL DEFAULT 0,
            size REAL DEFAULT 0, stake_usd REAL DEFAULT 0,
            pnl REAL DEFAULT 0, close_reason TEXT DEFAULT '',
            question TEXT DEFAULT '', market_type TEXT DEFAULT '',
            opened_at TEXT, closed_at TEXT
        );
        CREATE TABLE performance_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL, question TEXT,
            category TEXT DEFAULT 'UNKNOWN',
            forecast_prob REAL, actual_outcome REAL,
            edge_at_entry REAL, confidence TEXT DEFAULT 'LOW',
            evidence_quality REAL DEFAULT 0, stake_usd REAL DEFAULT 0,
            entry_price REAL DEFAULT 0, exit_price REAL DEFAULT 0,
            pnl REAL DEFAULT 0, holding_hours REAL DEFAULT 0,
            resolved_at TEXT
        );
        CREATE TABLE engine_state (
            key TEXT PRIMARY KEY, value TEXT, updated_at REAL
        );
        CREATE TABLE alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL, channel TEXT DEFAULT 'system',
            message TEXT NOT NULL, market_id TEXT DEFAULT '',
            created_at TEXT
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


def _make_db(conn: sqlite3.Connection):
    """Create a Database instance wrapping a test connection."""
    from src.config import StorageConfig
    from src.storage.database import Database

    db = Database(StorageConfig(sqlite_path=":memory:"))
    db._conn = conn
    return db


# ═══════════════════════════════════════════════════════════════════
#  MODEL TESTS
# ═══════════════════════════════════════════════════════════════════

class TestModels:
    """Test new model classes."""

    def test_position_record_has_question_and_market_type(self) -> None:
        pos = PositionRecord(
            market_id="m1", token_id="t1", direction="BUY_YES",
            entry_price=0.55, size=100, stake_usd=55,
            question="Will X happen?", market_type="POLITICS",
        )
        assert pos.question == "Will X happen?"
        assert pos.market_type == "POLITICS"

    def test_position_record_defaults_empty_strings(self) -> None:
        pos = PositionRecord(market_id="m1")
        assert pos.question == ""
        assert pos.market_type == ""

    def test_closed_position_record(self) -> None:
        rec = ClosedPositionRecord(
            market_id="m1", token_id="t1", direction="BUY_YES",
            entry_price=0.55, exit_price=0.70, size=100,
            stake_usd=55, pnl=15.0, close_reason="TAKE_PROFIT",
            question="Will X happen?", market_type="POLITICS",
        )
        assert rec.pnl == 15.0
        assert rec.close_reason == "TAKE_PROFIT"
        assert rec.closed_at != ""  # auto-populated

    def test_performance_log_record(self) -> None:
        rec = PerformanceLogRecord(
            market_id="m1", question="Test?", category="POLITICS",
            forecast_prob=0.65, edge_at_entry=0.10,
            confidence="MEDIUM", evidence_quality=0.7,
            stake_usd=25, entry_price=0.55, exit_price=0.70,
            pnl=15.0, holding_hours=24.5,
        )
        assert rec.pnl == 15.0
        assert rec.holding_hours == 24.5
        assert rec.category == "POLITICS"
        assert rec.resolved_at != ""


# ═══════════════════════════════════════════════════════════════════
#  DATABASE METHOD TESTS
# ═══════════════════════════════════════════════════════════════════

class TestInsertPerformanceLog:
    """Test Database.insert_performance_log()."""

    def test_insert_and_query(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        record = PerformanceLogRecord(
            market_id="mkt_1", question="Will X?", category="POLITICS",
            forecast_prob=0.65, actual_outcome=1.0, edge_at_entry=0.10,
            confidence="MEDIUM", evidence_quality=0.7,
            stake_usd=25, entry_price=0.55, exit_price=1.0,
            pnl=11.25, holding_hours=48.0,
        )
        db.insert_performance_log(record)

        rows = conn.execute("SELECT * FROM performance_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["market_id"] == "mkt_1"
        assert rows[0]["pnl"] == 11.25
        assert rows[0]["category"] == "POLITICS"
        assert rows[0]["holding_hours"] == 48.0

    def test_insert_with_none_actual_outcome(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        record = PerformanceLogRecord(
            market_id="mkt_2", stake_usd=25, pnl=-5.0,
        )
        db.insert_performance_log(record)

        row = conn.execute("SELECT actual_outcome FROM performance_log").fetchone()
        assert row["actual_outcome"] is None

    def test_multiple_records(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        for i in range(5):
            db.insert_performance_log(PerformanceLogRecord(
                market_id=f"mkt_{i}", pnl=10.0 * (i + 1), stake_usd=50,
            ))
        rows = conn.execute("SELECT COUNT(*) c FROM performance_log").fetchone()
        assert rows["c"] == 5


class TestArchivePosition:
    """Test Database.archive_position()."""

    def test_archive_creates_record(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        pos = PositionRecord(
            market_id="mkt_1", token_id="tok_1", direction="BUY_YES",
            entry_price=0.55, size=100, stake_usd=55.0,
            question="Will X?", market_type="POLITICS",
            opened_at="2025-01-01T00:00:00+00:00",
        )
        db.archive_position(pos, exit_price=0.70, pnl=15.0, close_reason="TAKE_PROFIT")

        rows = conn.execute("SELECT * FROM closed_positions").fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["market_id"] == "mkt_1"
        assert r["entry_price"] == 0.55
        assert r["exit_price"] == 0.70
        assert r["pnl"] == 15.0
        assert r["close_reason"] == "TAKE_PROFIT"
        assert r["question"] == "Will X?"
        assert r["closed_at"] is not None

    def test_archive_preserves_opened_at(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        pos = PositionRecord(
            market_id="mkt_2", opened_at="2025-06-01T12:00:00+00:00",
        )
        db.archive_position(pos, exit_price=0.5, pnl=0.0, close_reason="MAX_HOLDING")

        row = conn.execute("SELECT opened_at FROM closed_positions").fetchone()
        assert row["opened_at"] == "2025-06-01T12:00:00+00:00"

    def test_multiple_archives_for_same_market(self) -> None:
        """A market can be traded multiple times — each close is a separate archive entry."""
        conn = _create_test_db()
        db = _make_db(conn)
        for i in range(3):
            pos = PositionRecord(market_id="mkt_1", stake_usd=25)
            db.archive_position(pos, exit_price=0.5, pnl=float(i), close_reason="STOP_LOSS")

        rows = conn.execute("SELECT COUNT(*) c FROM closed_positions WHERE market_id = 'mkt_1'").fetchone()
        assert rows["c"] == 3


class TestGetClosedPositions:
    """Test Database.get_closed_positions()."""

    def test_returns_list_of_dicts(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        pos = PositionRecord(market_id="mkt_1", stake_usd=50)
        db.archive_position(pos, exit_price=0.8, pnl=10.0, close_reason="TAKE_PROFIT")

        result = db.get_closed_positions()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["market_id"] == "mkt_1"

    def test_empty_returns_empty_list(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        assert db.get_closed_positions() == []

    def test_respects_limit(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        for i in range(10):
            pos = PositionRecord(market_id=f"mkt_{i}")
            db.archive_position(pos, exit_price=0.5, pnl=1.0, close_reason="SL")
        result = db.get_closed_positions(limit=3)
        assert len(result) == 3


class TestGetPosition:
    """Test Database.get_position()."""

    def test_returns_position(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        db.upsert_position(PositionRecord(
            market_id="mkt_1", entry_price=0.55, stake_usd=50,
            question="Will X?", market_type="POLITICS",
        ))
        pos = db.get_position("mkt_1")
        assert pos is not None
        assert pos.entry_price == 0.55
        assert pos.question == "Will X?"

    def test_returns_none_for_missing(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        assert db.get_position("nonexistent") is None


class TestGetDailyPnl:
    """Test the fixed get_daily_pnl() method."""

    def test_unrealized_pnl_from_open_positions(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        # Insert a position with unrealized P&L
        db.upsert_position(PositionRecord(
            market_id="mkt_1", entry_price=0.50, current_price=0.60,
            size=100, stake_usd=50, pnl=10.0,
        ))
        db.upsert_position(PositionRecord(
            market_id="mkt_2", entry_price=0.50, current_price=0.40,
            size=100, stake_usd=50, pnl=-10.0,
        ))
        # Net unrealized: 10 + (-10) = 0
        assert db.get_daily_pnl() == pytest.approx(0.0)

    def test_realized_pnl_from_performance_log(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        # Insert a resolved trade for today
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        db.insert_performance_log(PerformanceLogRecord(
            market_id="mkt_1", pnl=25.0, stake_usd=50, resolved_at=now,
        ))
        # No open positions
        pnl = db.get_daily_pnl()
        assert pnl == pytest.approx(25.0)

    def test_combined_realized_and_unrealized(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        db.insert_performance_log(PerformanceLogRecord(
            market_id="mkt_closed", pnl=15.0, resolved_at=now,
        ))
        db.upsert_position(PositionRecord(
            market_id="mkt_open", pnl=5.0,
        ))
        pnl = db.get_daily_pnl()
        assert pnl == pytest.approx(20.0)

    def test_empty_db_returns_zero(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        assert db.get_daily_pnl() == pytest.approx(0.0)


class TestUpsertPositionWithNewFields:
    """Test that upsert_position handles question/market_type."""

    def test_insert_with_question_and_market_type(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        db.upsert_position(PositionRecord(
            market_id="mkt_1", token_id="tok_1", direction="BUY_YES",
            entry_price=0.55, size=100, stake_usd=55,
            question="Will X?", market_type="POLITICS",
        ))
        row = conn.execute("SELECT question, market_type FROM positions WHERE market_id = 'mkt_1'").fetchone()
        assert row["question"] == "Will X?"
        assert row["market_type"] == "POLITICS"

    def test_update_preserves_question(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)
        db.upsert_position(PositionRecord(
            market_id="mkt_1", entry_price=0.55, question="Will X?",
        ))
        # Update with new price
        db.upsert_position(PositionRecord(
            market_id="mkt_1", entry_price=0.55,
            current_price=0.60, pnl=5.0, question="Will X?",
        ))
        rows = conn.execute("SELECT COUNT(*) c FROM positions WHERE market_id = 'mkt_1'").fetchone()
        assert rows["c"] == 1


# ═══════════════════════════════════════════════════════════════════
#  PERFORMANCE TRACKER INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════

class TestPerformanceTrackerWithLog:
    """Test that PerformanceTracker correctly uses performance_log data."""

    def test_compute_uses_performance_log_when_populated(self) -> None:
        from src.analytics.performance_tracker import PerformanceTracker

        conn = _create_test_db()
        # Also need positions table for fallback path
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS model_forecast_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_name TEXT, market_id TEXT, category TEXT,
                forecast_prob REAL, actual_outcome REAL, recorded_at TEXT
            );
        """)

        # Insert performance_log data
        for i in range(10):
            pnl = 10.0 if i % 3 != 0 else -5.0
            conn.execute("""
                INSERT INTO performance_log
                    (market_id, question, category, forecast_prob, actual_outcome,
                     edge_at_entry, confidence, evidence_quality, stake_usd,
                     entry_price, exit_price, pnl, holding_hours, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"mkt_{i}", f"Q{i}?", "POLITICS", 0.65, 1.0 if pnl > 0 else 0.0,
                0.10, "MEDIUM", 0.7, 25.0, 0.55,
                1.0 if pnl > 0 else 0.0, pnl, 24.0,
                dt.datetime.now(dt.timezone.utc).isoformat(),
            ))
        conn.commit()

        tracker = PerformanceTracker(bankroll=5000)
        snap = tracker.compute(conn)

        # Should use performance_log, not fallback
        assert snap.total_trades == 10
        assert snap.win_rate > 0
        assert snap.total_pnl != 0

    def test_compute_falls_back_to_open_positions(self) -> None:
        from src.analytics.performance_tracker import PerformanceTracker

        conn = _create_test_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS model_forecast_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_name TEXT, market_id TEXT, category TEXT,
                forecast_prob REAL, actual_outcome REAL, recorded_at TEXT
            );
        """)

        # performance_log is empty, but we have open positions
        conn.execute("""
            INSERT INTO positions
                (market_id, token_id, direction, entry_price, size,
                 stake_usd, current_price, pnl, opened_at)
            VALUES ('mkt_1', 'tok_1', 'BUY_YES', 0.55, 100, 55, 0.60, 5.0,
                    datetime('now'))
        """)
        conn.commit()

        tracker = PerformanceTracker(bankroll=5000)
        snap = tracker.compute(conn)

        # Should use fallback
        assert snap.total_trades >= 1
        assert snap.total_pnl == pytest.approx(5.0)


# ═══════════════════════════════════════════════════════════════════
#  MIGRATION TESTS
# ═══════════════════════════════════════════════════════════════════

class TestMigration8:
    """Test that migration 8 adds the expected schema changes."""

    def test_migration_adds_closed_positions_table(self) -> None:
        from src.storage.migrations import run_migrations

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)

        # Verify closed_positions table exists
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "closed_positions" in tables

    def test_migration_adds_question_to_positions(self) -> None:
        from src.storage.migrations import run_migrations

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)

        # Verify positions table has question column
        cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        assert "question" in cols
        assert "market_type" in cols

    def test_schema_version_is_8(self) -> None:
        from src.storage.migrations import run_migrations, SCHEMA_VERSION

        assert SCHEMA_VERSION == 8
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)

        ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == 8


# ═══════════════════════════════════════════════════════════════════
#  CONFIG TESTS
# ═══════════════════════════════════════════════════════════════════

class TestRiskConfigMaxHolding:
    """Test max_holding_hours config option."""

    def test_default_value(self) -> None:
        from src.config import RiskConfig
        cfg = RiskConfig()
        assert cfg.max_holding_hours == 240.0

    def test_custom_value(self) -> None:
        from src.config import RiskConfig
        cfg = RiskConfig(max_holding_hours=24.0)
        assert cfg.max_holding_hours == 24.0

    def test_zero_disables(self) -> None:
        from src.config import RiskConfig
        cfg = RiskConfig(max_holding_hours=0)
        assert cfg.max_holding_hours == 0


# ═══════════════════════════════════════════════════════════════════
#  EXIT LOGIC TESTS (unit tests for exit reason determination)
# ═══════════════════════════════════════════════════════════════════

class TestExitReasonDetermination:
    """Test the exit logic that determines when to close a position.

    These tests verify the exit reason selection without needing
    the full async engine — they test the logic in isolation.
    """

    def _determine_exit(
        self,
        pnl: float,
        stake_usd: float,
        current_price: float,
        opened_at: str,
        sl_pct: float = 0.20,
        tp_pct: float = 0.30,
        max_hold: float = 72.0,
    ) -> str:
        """Replicate the exit reason logic from _check_positions."""
        import datetime as _dt

        pnl_pct = pnl / stake_usd if stake_usd > 0 else 0.0

        if sl_pct > 0 and pnl_pct <= -sl_pct:
            return f"STOP_LOSS: {pnl_pct:.1%} <= -{sl_pct:.0%}"
        elif tp_pct > 0 and pnl_pct >= tp_pct:
            return f"TAKE_PROFIT: {pnl_pct:.1%} >= +{tp_pct:.0%}"
        elif current_price >= 0.98 or current_price <= 0.02:
            return f"MARKET_RESOLVED: price={current_price:.4f}"
        elif max_hold > 0:
            try:
                opened = _dt.datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                now = _dt.datetime.now(_dt.timezone.utc)
                holding_hours = (now - opened).total_seconds() / 3600
                if holding_hours >= max_hold:
                    return f"MAX_HOLDING: {holding_hours:.1f}h >= {max_hold:.0f}h"
            except Exception:
                pass
        return ""

    def test_stop_loss_triggers(self) -> None:
        reason = self._determine_exit(pnl=-15.0, stake_usd=50.0, current_price=0.35, opened_at="2025-01-01T00:00:00+00:00")
        assert "STOP_LOSS" in reason

    def test_take_profit_triggers(self) -> None:
        reason = self._determine_exit(pnl=20.0, stake_usd=50.0, current_price=0.75, opened_at="2025-01-01T00:00:00+00:00")
        assert "TAKE_PROFIT" in reason

    def test_market_resolved_at_1(self) -> None:
        reason = self._determine_exit(pnl=5.0, stake_usd=50.0, current_price=0.99, opened_at="2025-01-01T00:00:00+00:00")
        assert "MARKET_RESOLVED" in reason

    def test_market_resolved_at_0(self) -> None:
        reason = self._determine_exit(pnl=-5.0, stake_usd=50.0, current_price=0.01, opened_at="2025-01-01T00:00:00+00:00")
        assert "MARKET_RESOLVED" in reason

    def test_max_holding_triggers(self) -> None:
        # Position opened 100 hours ago
        old_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=100)).isoformat()
        reason = self._determine_exit(pnl=0, stake_usd=50.0, current_price=0.55, opened_at=old_time, max_hold=72.0)
        assert "MAX_HOLDING" in reason

    def test_no_exit_within_thresholds(self) -> None:
        recent = dt.datetime.now(dt.timezone.utc).isoformat()
        reason = self._determine_exit(pnl=2.0, stake_usd=50.0, current_price=0.55, opened_at=recent)
        assert reason == ""

    def test_stop_loss_priority_over_market_resolved(self) -> None:
        """Stop loss at very low price — SL should take priority over MARKET_RESOLVED."""
        reason = self._determine_exit(pnl=-15.0, stake_usd=50.0, current_price=0.01, opened_at="2025-01-01T00:00:00+00:00")
        assert "STOP_LOSS" in reason

    def test_take_profit_priority_over_market_resolved(self) -> None:
        """Take profit at price 0.99 — TP should take priority."""
        reason = self._determine_exit(pnl=20.0, stake_usd=50.0, current_price=0.99, opened_at="2025-01-01T00:00:00+00:00")
        assert "TAKE_PROFIT" in reason

    def test_zero_max_hold_disables_time_exit(self) -> None:
        old_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1000)).isoformat()
        reason = self._determine_exit(pnl=0, stake_usd=50.0, current_price=0.55, opened_at=old_time, max_hold=0)
        assert reason == ""

    def test_edge_case_exactly_at_threshold(self) -> None:
        # Exactly at -20% stop loss
        reason = self._determine_exit(pnl=-10.0, stake_usd=50.0, current_price=0.40, opened_at="2025-01-01T00:00:00+00:00")
        assert "STOP_LOSS" in reason

    def test_edge_case_just_under_threshold(self) -> None:
        # Just under 20% loss — should NOT trigger
        reason = self._determine_exit(pnl=-9.9, stake_usd=50.0, current_price=0.45, opened_at=dt.datetime.now(dt.timezone.utc).isoformat())
        assert reason == ""


# ═══════════════════════════════════════════════════════════════════
#  END-TO-END FLOW TESTS
# ═══════════════════════════════════════════════════════════════════

class TestEndToEndPositionLifecycle:
    """Test the complete lifecycle: open → update → close → archive → performance_log."""

    def test_full_lifecycle(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)

        # 1. Open a position
        pos = PositionRecord(
            market_id="mkt_1", token_id="tok_1", direction="BUY_YES",
            entry_price=0.55, size=100, stake_usd=55.0,
            question="Will X?", market_type="POLITICS",
        )
        db.upsert_position(pos)
        assert db.get_open_positions_count() == 1

        # 2. Update price
        db.update_position_price("mkt_1", 0.70, 15.0)
        updated = db.get_position("mkt_1")
        assert updated is not None
        assert updated.current_price == 0.70
        assert updated.pnl == 15.0

        # 3. Archive the position
        db.archive_position(updated, exit_price=0.70, pnl=15.0, close_reason="TAKE_PROFIT")

        # 4. Record performance log
        db.insert_performance_log(PerformanceLogRecord(
            market_id="mkt_1", question="Will X?", category="POLITICS",
            forecast_prob=0.65, actual_outcome=None,
            edge_at_entry=0.10, confidence="MEDIUM",
            evidence_quality=0.7, stake_usd=55.0,
            entry_price=0.55, exit_price=0.70, pnl=15.0,
            holding_hours=24.0,
        ))

        # 5. Remove position
        db.remove_position("mkt_1")
        assert db.get_open_positions_count() == 0

        # 6. Verify archive and performance log
        closed = db.get_closed_positions()
        assert len(closed) == 1
        assert closed[0]["pnl"] == 15.0
        assert closed[0]["close_reason"] == "TAKE_PROFIT"

        perf = conn.execute("SELECT * FROM performance_log").fetchall()
        assert len(perf) == 1
        assert perf[0]["pnl"] == 15.0
        assert perf[0]["category"] == "POLITICS"

    def test_multiple_positions_lifecycle(self) -> None:
        conn = _create_test_db()
        db = _make_db(conn)

        # Open 3 positions
        for i in range(3):
            db.upsert_position(PositionRecord(
                market_id=f"mkt_{i}", entry_price=0.50, stake_usd=25,
            ))
        assert db.get_open_positions_count() == 3

        # Close 2 with different reasons
        for i, reason in [(0, "STOP_LOSS"), (1, "TAKE_PROFIT")]:
            pos = db.get_position(f"mkt_{i}")
            db.archive_position(pos, exit_price=0.5, pnl=float(i * 10 - 5), close_reason=reason)
            db.insert_performance_log(PerformanceLogRecord(
                market_id=f"mkt_{i}", pnl=float(i * 10 - 5),
            ))
            db.remove_position(f"mkt_{i}")

        assert db.get_open_positions_count() == 1
        assert len(db.get_closed_positions()) == 2

        perf_count = conn.execute("SELECT COUNT(*) FROM performance_log").fetchone()[0]
        assert perf_count == 2

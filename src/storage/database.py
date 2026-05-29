"""Database — SQLite persistence layer.

Manages connections, runs migrations, and provides CRUD operations.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from src.config import StorageConfig
from src.storage.migrations import run_migrations
from src.storage.models import (
    ClosedPositionRecord,
    ForecastRecord,
    MarketRecord,
    PerformanceLogRecord,
    PositionRecord,
    TradeRecord,
)
from src.observability.logger import get_logger

log = get_logger(__name__)


class Database:
    """SQLite database for the bot."""

    def __init__(self, config: StorageConfig):
        self._config = config
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        """Open database connection and run migrations."""
        db_path = Path(self._config.sqlite_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        run_migrations(self._conn)
        log.info("database.connected", path=str(db_path))

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    # ── Markets ──────────────────────────────────────────────────────

    def upsert_market(self, market: MarketRecord) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO markets
                (id, condition_id, question, market_type, category,
                 volume, liquidity, end_date, resolution_source,
                 first_seen, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market.id, market.condition_id, market.question,
                market.market_type, market.category, market.volume,
                market.liquidity, market.end_date, market.resolution_source,
                market.first_seen, market.last_updated,
            ),
        )
        self.conn.commit()

    def get_market(self, market_id: str) -> MarketRecord | None:
        row = self.conn.execute(
            "SELECT * FROM markets WHERE id = ?", (market_id,)
        ).fetchone()
        if row:
            return MarketRecord(**dict(row))
        return None

    # ── Forecasts ────────────────────────────────────────────────────

    def insert_forecast(self, forecast: ForecastRecord) -> str:
        fid = forecast.id or str(uuid.uuid4())
        self.conn.execute(
            """
            INSERT INTO forecasts
                (id, market_id, question, market_type,
                 implied_probability, model_probability, edge,
                 confidence_level, evidence_quality, num_sources,
                 decision, reasoning, evidence_json,
                 invalidation_triggers_json, research_evidence_json,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fid, forecast.market_id, forecast.question,
                forecast.market_type, forecast.implied_probability,
                forecast.model_probability, forecast.edge,
                forecast.confidence_level, forecast.evidence_quality,
                forecast.num_sources, forecast.decision,
                forecast.reasoning, forecast.evidence_json,
                forecast.invalidation_triggers_json,
                forecast.research_evidence_json, forecast.created_at,
            ),
        )
        self.conn.commit()
        return fid

    def get_forecasts(
        self, market_id: str | None = None, limit: int = 50
    ) -> list[ForecastRecord]:
        if market_id:
            rows = self.conn.execute(
                "SELECT * FROM forecasts WHERE market_id = ? ORDER BY created_at DESC LIMIT ?",
                (market_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM forecasts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ForecastRecord(**dict(r)) for r in rows]

    # ── Trades ───────────────────────────────────────────────────────

    def insert_trade(self, trade: TradeRecord) -> str:
        tid = trade.id or str(uuid.uuid4())
        self.conn.execute(
            """
            INSERT INTO trades
                (id, order_id, market_id, token_id, side,
                 price, size, stake_usd, status, dry_run, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tid, trade.order_id, trade.market_id, trade.token_id,
                trade.side, trade.price, trade.size, trade.stake_usd,
                trade.status, int(trade.dry_run), trade.created_at,
            ),
        )
        self.conn.commit()
        return tid

    def get_daily_pnl(self) -> float:
        """Get total PnL for today: realized (closed positions) + unrealized (open positions)."""
        # Realized P&L from positions closed today
        realized = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM performance_log "
            "WHERE date(resolved_at) = date('now')"
        ).fetchone()
        realized_pnl = float(realized[0]) if realized else 0.0

        # Unrealized P&L from open positions
        unrealized = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM positions"
        ).fetchone()
        unrealized_pnl = float(unrealized[0]) if unrealized else 0.0

        return realized_pnl + unrealized_pnl

    # ── Positions ────────────────────────────────────────────────────

    def get_open_positions_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()
        return int(row[0]) if row else 0

    def get_open_positions(self) -> list[PositionRecord]:
        """Return all open positions as PositionRecord objects."""
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return [PositionRecord(**dict(r)) for r in rows]

    def upsert_position(self, pos: PositionRecord) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO positions
                (market_id, token_id, direction, entry_price,
                 size, stake_usd, current_price, pnl, opened_at,
                 question, market_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pos.market_id, pos.token_id, pos.direction,
                pos.entry_price, pos.size, pos.stake_usd,
                pos.current_price, pos.pnl, pos.opened_at,
                pos.question, pos.market_type,
            ),
        )
        self.conn.commit()

    def update_position_price(self, market_id: str, current_price: float, pnl: float) -> None:
        """Update current price and PNL for a position."""
        self.conn.execute(
            "UPDATE positions SET current_price = ?, pnl = ? WHERE market_id = ?",
            (current_price, pnl, market_id),
        )
        self.conn.commit()

    def remove_position(self, market_id: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE market_id = ?", (market_id,))
        self.conn.commit()

    def archive_position(
        self,
        pos: PositionRecord,
        exit_price: float,
        pnl: float,
        close_reason: str,
    ) -> None:
        """Save a closing position to the closed_positions archive before deletion."""
        import datetime as _dt
        try:
            self.conn.execute(
                """INSERT INTO closed_positions
                    (market_id, token_id, direction, entry_price, exit_price,
                     size, stake_usd, pnl, close_reason, question, market_type,
                     opened_at, closed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pos.market_id, pos.token_id, pos.direction,
                    pos.entry_price, exit_price, pos.size, pos.stake_usd,
                    pnl, close_reason,
                    getattr(pos, "question", ""),
                    getattr(pos, "market_type", ""),
                    pos.opened_at,
                    _dt.datetime.now(_dt.timezone.utc).isoformat(),
                ),
            )
            self.conn.commit()
        except Exception as e:
            log.warning("database.archive_position_error", error=str(e))

    def insert_performance_log(self, record: PerformanceLogRecord) -> None:
        """Insert a resolved trade into the performance_log table."""
        try:
            self.conn.execute(
                """INSERT INTO performance_log
                    (market_id, question, category, forecast_prob, actual_outcome,
                     edge_at_entry, confidence, evidence_quality, stake_usd,
                     entry_price, exit_price, pnl, holding_hours, resolved_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.market_id, record.question, record.category,
                    record.forecast_prob, record.actual_outcome,
                    record.edge_at_entry, record.confidence,
                    record.evidence_quality, record.stake_usd,
                    record.entry_price, record.exit_price, record.pnl,
                    record.holding_hours, record.resolved_at,
                ),
            )
            self.conn.commit()
        except Exception as e:
            log.warning("database.insert_performance_log_error", error=str(e))

    def get_closed_positions(self, limit: int = 100) -> list[dict]:
        """Return closed positions, most recent first."""
        rows = self.conn.execute(
            "SELECT * FROM closed_positions ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_position(self, market_id: str) -> PositionRecord | None:
        """Return a single position by market_id, or None."""
        row = self.conn.execute(
            "SELECT * FROM positions WHERE market_id = ?", (market_id,)
        ).fetchone()
        if row:
            d = dict(row)
            # Handle older schema without question/market_type columns
            d.setdefault("question", "")
            d.setdefault("market_type", "")
            return PositionRecord(**d)
        return None

    # ── Engine State ─────────────────────────────────────────────────

    def set_engine_state(self, key: str, value: str) -> None:
        """Persist engine state (for cross-process dashboard reads)."""
        import time as _time
        self.conn.execute(
            "INSERT OR REPLACE INTO engine_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, _time.time()),
        )
        self.conn.commit()

    def get_engine_state(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM engine_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def get_all_engine_state(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM engine_state").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ── Candidate Log ────────────────────────────────────────────────

    def insert_candidate(
        self,
        cycle_id: int,
        market_id: str,
        question: str,
        market_type: str,
        implied_prob: float,
        model_prob: float,
        edge: float,
        evidence_quality: float,
        num_sources: int,
        confidence: str,
        decision: str,
        decision_reasons: str,
        stake_usd: float,
        order_status: str,
    ) -> None:
        import datetime as _dt
        self.conn.execute(
            """INSERT INTO candidates
                (cycle_id, market_id, question, market_type, implied_prob,
                 model_prob, edge, evidence_quality, num_sources, confidence,
                 decision, decision_reasons, stake_usd, order_status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cycle_id, market_id, question, market_type, implied_prob,
                model_prob, edge, evidence_quality, num_sources, confidence,
                decision, decision_reasons, stake_usd, order_status,
                _dt.datetime.now(_dt.timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def get_candidates(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM candidates ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Alerts Log ───────────────────────────────────────────────────

    def insert_alert(self, level: str, message: str, channel: str = "system", market_id: str = "") -> None:
        import datetime as _dt
        self.conn.execute(
            "INSERT INTO alerts_log (level, channel, message, market_id, created_at) VALUES (?,?,?,?,?)",
            (level, channel, message, market_id, _dt.datetime.now(_dt.timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_alerts(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM alerts_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Trades (extended) ────────────────────────────────────────────

    def get_trades(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Watchlist ────────────────────────────────────────────────────

    def add_to_watchlist(self, market_id: str, question: str, category: str = "", notes: str = "") -> None:
        import datetime as _dt
        self.conn.execute(
            "INSERT OR REPLACE INTO watchlist (market_id, question, category, added_at, notes) VALUES (?,?,?,?,?)",
            (market_id, question, category, _dt.datetime.now(_dt.timezone.utc).isoformat(), notes),
        )
        self.conn.commit()

    def remove_from_watchlist(self, market_id: str) -> None:
        self.conn.execute("DELETE FROM watchlist WHERE market_id = ?", (market_id,))
        self.conn.commit()

    def get_watchlist(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]

    def is_on_watchlist(self, market_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM watchlist WHERE market_id = ?", (market_id,)).fetchone()
        return row is not None

    # ── Trade Journal ────────────────────────────────────────────────

    def insert_journal_entry(
        self, market_id: str, question: str, direction: str,
        entry_price: float, exit_price: float, stake_usd: float, pnl: float,
        annotation: str = "", reasoning: str = "", lessons_learned: str = "",
        tags: str = "[]",
    ) -> int:
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT INTO trade_journal
                (market_id, question, direction, entry_price, exit_price,
                 stake_usd, pnl, annotation, reasoning, lessons_learned, tags,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (market_id, question, direction, entry_price, exit_price,
             stake_usd, pnl, annotation, reasoning, lessons_learned, tags,
             now, now),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def update_journal_annotation(self, journal_id: int, annotation: str, lessons_learned: str = "") -> None:
        import datetime as _dt
        self.conn.execute(
            "UPDATE trade_journal SET annotation = ?, lessons_learned = ?, updated_at = ? WHERE id = ?",
            (annotation, lessons_learned, _dt.datetime.now(_dt.timezone.utc).isoformat(), journal_id),
        )
        self.conn.commit()

    def get_journal_entries(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trade_journal ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Equity Snapshots ─────────────────────────────────────────────

    def insert_equity_snapshot(
        self, equity: float, invested: float, cash: float,
        unrealised_pnl: float, realised_pnl: float, num_positions: int,
        daily_var: float = 0.0, drawdown_pct: float = 0.0,
    ) -> None:
        import datetime as _dt
        self.conn.execute(
            """INSERT INTO equity_snapshots
                (timestamp, equity, invested, cash, unrealised_pnl, realised_pnl,
                 num_positions, daily_var, drawdown_pct)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (_dt.datetime.now(_dt.timezone.utc).isoformat(), equity, invested, cash,
             unrealised_pnl, realised_pnl, num_positions, daily_var, drawdown_pct),
        )
        self.conn.commit()

    def get_equity_snapshots(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM equity_snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed([dict(r) for r in rows])]

    # ── VaR History ──────────────────────────────────────────────────

    def insert_var_record(
        self, daily_var_95: float, daily_var_99: float,
        portfolio_value: float, num_positions: int,
        method: str = "parametric", details_json: str = "{}",
    ) -> None:
        import datetime as _dt
        self.conn.execute(
            """INSERT INTO var_history
                (timestamp, daily_var_95, daily_var_99, portfolio_value,
                 num_positions, method, details_json)
            VALUES (?,?,?,?,?,?,?)""",
            (_dt.datetime.now(_dt.timezone.utc).isoformat(), daily_var_95, daily_var_99,
             portfolio_value, num_positions, method, details_json),
        )
        self.conn.commit()

    def get_var_history(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM var_history ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Calibration History ──────────────────────────────────────────

    def get_calibration_history(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM calibration_history ORDER BY recorded_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Maintenance ──────────────────────────────────────────────────

    def vacuum(self) -> None:
        """Run VACUUM to reclaim space and optimize the database."""
        try:
            self.conn.execute("VACUUM")
            log.info("database.vacuum_complete")
        except Exception as e:
            log.warning("database.vacuum_error", error=str(e))

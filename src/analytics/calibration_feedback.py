"""Calibration feedback loop — learn from resolved markets.

Closes the loop between forecasts and outcomes:
  1. When a market resolves, record (forecast_prob, actual_outcome)
  2. Periodically retrain the HistoricalCalibrator with new data
  3. Log per-model accuracy for adaptive weighting
  4. Persist all calibration data to DB

This transforms the bot from a static forecaster into an
adaptive, self-improving system.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from src.forecast.calibrator import get_historical_calibrator
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class ResolutionRecord:
    """Record of a market resolution and its associated forecast."""
    market_id: str
    question: str
    category: str
    forecast_prob: float
    actual_outcome: float  # 1.0 = YES resolved, 0.0 = NO resolved
    edge_at_entry: float
    confidence: str
    evidence_quality: float
    stake_usd: float
    entry_price: float
    exit_price: float
    pnl: float
    holding_hours: float
    model_forecasts: dict[str, float] = field(default_factory=dict)
    resolved_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class CalibrationFeedbackLoop:
    """Manages the feedback loop between forecasts and outcomes.

    On each resolution:
      1. Records calibration history (forecast_prob → outcome)
      2. Records per-model forecast accuracy (model_forecast_log)
      3. Records trade performance (performance_log)
      4. Retrains the global calibrator when enough new data arrives
    """

    def __init__(self, retrain_interval: int = 10):
        """
        Args:
            retrain_interval: Retrain calibrator every N new resolutions.
        """
        self._retrain_interval = retrain_interval
        self._since_last_retrain = 0

    def record_resolution(
        self,
        conn: sqlite3.Connection,
        record: ResolutionRecord,
    ) -> None:
        """Record a market resolution and update all tracking tables."""
        now_iso = record.resolved_at or _now_iso()

        # 1. Calibration history
        self._insert_calibration_history(conn, record, now_iso)

        # 2. Per-model forecast log (for adaptive weighting)
        self._insert_model_forecasts(conn, record, now_iso)

        # 3. Performance log (for analytics)
        self._insert_performance_log(conn, record, now_iso)

        # 4. Check if we should retrain
        self._since_last_retrain += 1
        if self._since_last_retrain >= self._retrain_interval:
            self.retrain_calibrator(conn)
            self._since_last_retrain = 0

        log.info(
            "calibration.resolution_recorded",
            market_id=record.market_id,
            forecast=round(record.forecast_prob, 3),
            outcome=record.actual_outcome,
            pnl=round(record.pnl, 2),
        )

    def retrain_calibrator(self, conn: sqlite3.Connection) -> bool:
        """Load all calibration history and retrain the global calibrator."""
        from src.forecast.calibrator import CalibrationHistory as CalHist

        calibrator = get_historical_calibrator()

        try:
            rows = conn.execute("""
                SELECT forecast_prob, actual_outcome
                FROM calibration_history
                ORDER BY recorded_at ASC
            """).fetchall()
        except sqlite3.OperationalError:
            log.warning("calibration.no_history_table")
            return False

        if len(rows) < 30:
            log.info(
                "calibration.insufficient_data",
                samples=len(rows),
                required=30,
            )
            return False

        history = [
            CalHist(
                forecast_prob=float(r["forecast_prob"]),
                actual_outcome=float(r["actual_outcome"]),
            )
            for r in rows
        ]

        success = calibrator.fit(history)

        if success:
            stats = calibrator.stats
            log.info(
                "calibration.retrained",
                samples=stats["n_samples"],
                brier=stats["brier_score"],
                a=stats["a"],
                b=stats["b"],
            )

            # Persist calibrator state to DB
            self._save_calibrator_state(conn, stats)

        return success

    def get_model_weights(
        self, conn: sqlite3.Connection, category: str = "ALL"
    ) -> dict[str, float]:
        """Compute adaptive model weights based on historical accuracy.

        Returns weights normalized to sum to 1.0, inversely proportional
        to each model's Brier score (lower = better = higher weight).
        """
        try:
            if category == "ALL":
                rows = conn.execute("""
                    SELECT model_name,
                           AVG((forecast_prob - actual_outcome) *
                               (forecast_prob - actual_outcome)) as brier,
                           COUNT(*) as cnt
                    FROM model_forecast_log
                    GROUP BY model_name
                    HAVING cnt >= 5
                """).fetchall()
            else:
                rows = conn.execute("""
                    SELECT model_name,
                           AVG((forecast_prob - actual_outcome) *
                               (forecast_prob - actual_outcome)) as brier,
                           COUNT(*) as cnt
                    FROM model_forecast_log
                    WHERE category = ?
                    GROUP BY model_name
                    HAVING cnt >= 5
                """, (category,)).fetchall()
        except sqlite3.OperationalError:
            return {}

        if not rows:
            return {}

        # Inverse Brier weighting: lower Brier = higher weight
        weights: dict[str, float] = {}
        for r in rows:
            brier = float(r["brier"])
            # Avoid division by zero; cap at minimum Brier of 0.001
            inv_brier = 1.0 / max(brier, 0.001)
            weights[r["model_name"]] = inv_brier

        # Normalize
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}

        log.info(
            "calibration.adaptive_weights",
            category=category,
            weights={k: round(v, 3) for k, v in weights.items()},
        )
        return weights

    # ── Internal Methods ─────────────────────────────────────────────

    def _insert_calibration_history(
        self, conn: sqlite3.Connection, record: ResolutionRecord, ts: str
    ) -> None:
        try:
            conn.execute("""
                INSERT INTO calibration_history
                    (forecast_prob, actual_outcome, recorded_at, market_id)
                VALUES (?, ?, ?, ?)
            """, (record.forecast_prob, record.actual_outcome, ts, record.market_id))
            conn.commit()
        except sqlite3.OperationalError:
            log.warning("calibration.missing_table", table="calibration_history")

    def _insert_model_forecasts(
        self, conn: sqlite3.Connection, record: ResolutionRecord, ts: str
    ) -> None:
        if not record.model_forecasts:
            return
        try:
            for model_name, prob in record.model_forecasts.items():
                conn.execute("""
                    INSERT INTO model_forecast_log
                        (model_name, market_id, category, forecast_prob,
                         actual_outcome, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    model_name, record.market_id, record.category,
                    prob, record.actual_outcome, ts,
                ))
            conn.commit()
        except sqlite3.OperationalError:
            log.warning("calibration.missing_table", table="model_forecast_log")

    def _insert_performance_log(
        self, conn: sqlite3.Connection, record: ResolutionRecord, ts: str
    ) -> None:
        try:
            conn.execute("""
                INSERT INTO performance_log
                    (market_id, question, category, forecast_prob,
                     actual_outcome, edge_at_entry, confidence,
                     evidence_quality, stake_usd, entry_price,
                     exit_price, pnl, holding_hours, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.market_id, record.question, record.category,
                record.forecast_prob, record.actual_outcome,
                record.edge_at_entry, record.confidence,
                record.evidence_quality, record.stake_usd,
                record.entry_price, record.exit_price,
                record.pnl, record.holding_hours, ts,
            ))
            conn.commit()
        except sqlite3.OperationalError:
            log.warning("calibration.missing_table", table="performance_log")

    def _save_calibrator_state(
        self, conn: sqlite3.Connection, stats: dict[str, Any]
    ) -> None:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO engine_state (key, value, updated_at)
                VALUES ('calibrator_state', ?, ?)
            """, (json.dumps(stats), time.time()))
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat()

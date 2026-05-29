"""Performance analytics engine — the intelligence layer.

Computes comprehensive trading metrics from historical data:
  - Win rate, ROI, profit factor
  - Sharpe ratio, Sortino ratio, Calmar ratio
  - Profit by market category
  - Model accuracy per category (for adaptive weighting)
  - Calibration accuracy (Brier score)
  - Equity curve data points
  - Strategy leaderboard (best-performing categories)
  - Rolling performance windows (7d, 30d, all-time)
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class CategoryStats:
    """Performance stats for a single market category."""
    category: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_staked: float = 0.0
    avg_edge: float = 0.0
    avg_evidence_quality: float = 0.0
    win_rate: float = 0.0
    roi_pct: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": round(self.total_pnl, 2),
            "total_staked": round(self.total_staked, 2),
            "avg_edge": round(self.avg_edge, 4),
            "avg_evidence_quality": round(self.avg_evidence_quality, 3),
            "win_rate": round(self.win_rate, 4),
            "roi_pct": round(self.roi_pct, 2),
            "best_trade_pnl": round(self.best_trade_pnl, 2),
            "worst_trade_pnl": round(self.worst_trade_pnl, 2),
        }


@dataclass
class ModelAccuracy:
    """Accuracy stats for a single LLM model, optionally per category."""
    model_name: str
    category: str = "ALL"
    total_forecasts: int = 0
    avg_error: float = 0.0  # mean absolute error vs outcome
    brier_score: float = 0.0
    calibration_slope: float = 1.0  # ideal = 1.0
    calibration_intercept: float = 0.0  # ideal = 0.0
    directional_accuracy: float = 0.0  # % of times direction was correct
    avg_confidence_when_right: float = 0.0
    avg_confidence_when_wrong: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "category": self.category,
            "total_forecasts": self.total_forecasts,
            "avg_error": round(self.avg_error, 4),
            "brier_score": round(self.brier_score, 4),
            "calibration_slope": round(self.calibration_slope, 4),
            "calibration_intercept": round(self.calibration_intercept, 4),
            "directional_accuracy": round(self.directional_accuracy, 4),
        }


@dataclass
class EquityPoint:
    """Single point on the equity curve."""
    timestamp: str
    equity: float
    pnl_cumulative: float
    drawdown_pct: float = 0.0
    trade_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "equity": round(self.equity, 2),
            "pnl_cumulative": round(self.pnl_cumulative, 2),
            "drawdown_pct": round(self.drawdown_pct, 4),
            "trade_count": self.trade_count,
        }


@dataclass
class PerformanceSnapshot:
    """Complete performance analytics snapshot."""
    # Overall metrics
    total_trades: int = 0
    total_forecasts: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_staked: float = 0.0
    roi_pct: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_holding_hours: float = 0.0

    # Risk-adjusted returns
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    calmar_ratio: float = 0.0
    avg_edge_captured: float = 0.0

    # Calibration
    brier_score: float = 0.0
    calibration_samples: int = 0

    # Streaks
    current_streak: int = 0  # positive = win streak, negative = loss streak
    best_streak: int = 0
    worst_streak: int = 0

    # Breakdowns
    category_stats: list[CategoryStats] = field(default_factory=list)
    model_accuracy: list[ModelAccuracy] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)

    # Rolling windows
    pnl_7d: float = 0.0
    pnl_30d: float = 0.0
    win_rate_7d: float = 0.0
    win_rate_30d: float = 0.0
    trades_7d: int = 0
    trades_30d: int = 0

    # Strategy leaderboard
    leaderboard: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "total_forecasts": self.total_forecasts,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "total_staked": round(self.total_staked, 2),
            "roi_pct": round(self.roi_pct, 2),
            "profit_factor": round(self.profit_factor, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "largest_win": round(self.largest_win, 2),
            "largest_loss": round(self.largest_loss, 2),
            "avg_holding_hours": round(self.avg_holding_hours, 1),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "sortino_ratio": round(self.sortino_ratio, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "calmar_ratio": round(self.calmar_ratio, 3),
            "avg_edge_captured": round(self.avg_edge_captured, 4),
            "brier_score": round(self.brier_score, 4),
            "calibration_samples": self.calibration_samples,
            "current_streak": self.current_streak,
            "best_streak": self.best_streak,
            "worst_streak": self.worst_streak,
            "pnl_7d": round(self.pnl_7d, 2),
            "pnl_30d": round(self.pnl_30d, 2),
            "win_rate_7d": round(self.win_rate_7d, 4),
            "win_rate_30d": round(self.win_rate_30d, 4),
            "trades_7d": self.trades_7d,
            "trades_30d": self.trades_30d,
            "category_stats": [c.to_dict() for c in self.category_stats],
            "model_accuracy": [m.to_dict() for m in self.model_accuracy],
            "equity_curve": [e.to_dict() for e in self.equity_curve],
            "leaderboard": self.leaderboard,
        }


class PerformanceTracker:
    """Compute comprehensive trading analytics from the database.

    This is read-only — it queries the DB tables populated by the engine
    and computes derived metrics on-the-fly.
    """

    def __init__(self, bankroll: float = 5000.0):
        self._bankroll = bankroll

    def compute(self, conn: sqlite3.Connection) -> PerformanceSnapshot:
        """Compute full performance snapshot from current DB state."""
        snap = PerformanceSnapshot()

        try:
            self._compute_trade_metrics(conn, snap)
            self._compute_forecast_metrics(conn, snap)
            self._compute_category_breakdown(conn, snap)
            self._compute_calibration(conn, snap)
            self._compute_equity_curve(conn, snap)
            self._compute_rolling_windows(conn, snap)
            self._compute_model_accuracy(conn, snap)

            # Fallback: when performance_log is empty (paper trading, no
            # resolved positions yet), compute analytics from open positions
            # so the dashboard still shows useful data.
            if snap.total_trades == 0:
                self._compute_from_open_positions(conn, snap)

            self._build_leaderboard(snap)
        except Exception as e:
            log.error("performance_tracker.compute_error", error=str(e))

        return snap

    # ── Trade Metrics ────────────────────────────────────────────────

    def _compute_trade_metrics(
        self, conn: sqlite3.Connection, snap: PerformanceSnapshot
    ) -> None:
        """Overall trade-level metrics: win rate, PnL, Sharpe, etc."""
        # Get resolved trades from performance_log table
        rows = self._query_safe(conn, """
            SELECT pnl, stake_usd, edge_at_entry, holding_hours, category,
                   resolved_at
            FROM performance_log
            ORDER BY resolved_at ASC
        """)

        if not rows:
            return

        pnls: list[float] = []
        stakes: list[float] = []
        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        total_hours = 0.0
        total_edge = 0.0
        current_streak = 0
        best_streak = 0
        worst_streak = 0

        for r in rows:
            pnl = float(r["pnl"] or 0)
            stake = float(r["stake_usd"] or 0)
            edge = float(r["edge_at_entry"] or 0)
            hours = float(r["holding_hours"] or 0)

            pnls.append(pnl)
            stakes.append(stake)
            total_hours += hours
            total_edge += edge

            if pnl > 0:
                wins += 1
                gross_profit += pnl
                if current_streak >= 0:
                    current_streak += 1
                else:
                    current_streak = 1
                best_streak = max(best_streak, current_streak)
            elif pnl < 0:
                losses += 1
                gross_loss += abs(pnl)
                if current_streak <= 0:
                    current_streak -= 1
                else:
                    current_streak = -1
                worst_streak = min(worst_streak, current_streak)

        n = len(pnls)
        snap.total_trades = n
        snap.win_rate = wins / n if n > 0 else 0.0
        snap.total_pnl = sum(pnls)
        snap.total_staked = sum(stakes)
        snap.roi_pct = (snap.total_pnl / snap.total_staked * 100) if snap.total_staked > 0 else 0.0
        snap.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
            float("inf") if gross_profit > 0 else 0.0
        )

        winning_pnls = [p for p in pnls if p > 0]
        losing_pnls = [p for p in pnls if p < 0]
        snap.avg_win = sum(winning_pnls) / len(winning_pnls) if winning_pnls else 0.0
        snap.avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0.0
        snap.largest_win = max(pnls) if pnls else 0.0
        snap.largest_loss = min(pnls) if pnls else 0.0
        snap.avg_holding_hours = total_hours / n if n > 0 else 0.0
        snap.avg_edge_captured = total_edge / n if n > 0 else 0.0

        snap.current_streak = current_streak
        snap.best_streak = best_streak
        snap.worst_streak = worst_streak

        # Sharpe ratio (daily returns, annualised)
        if n >= 2:
            mean_ret = sum(pnls) / n
            std_ret = math.sqrt(sum((p - mean_ret) ** 2 for p in pnls) / (n - 1))
            snap.sharpe_ratio = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0

            # Sortino (only downside deviation)
            downside = [p for p in pnls if p < 0]
            if downside:
                down_std = math.sqrt(sum(p ** 2 for p in downside) / len(downside))
                snap.sortino_ratio = (mean_ret / down_std * math.sqrt(252)) if down_std > 0 else 0.0

    # ── Forecast Metrics ─────────────────────────────────────────────

    def _compute_forecast_metrics(
        self, conn: sqlite3.Connection, snap: PerformanceSnapshot
    ) -> None:
        row = self._query_safe(conn, "SELECT COUNT(*) as cnt FROM forecasts")
        if row:
            snap.total_forecasts = int(row[0]["cnt"])

    # ── Category Breakdown ───────────────────────────────────────────

    def _compute_category_breakdown(
        self, conn: sqlite3.Connection, snap: PerformanceSnapshot
    ) -> None:
        rows = self._query_safe(conn, """
            SELECT category,
                   COUNT(*) as total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                   SUM(pnl) as total_pnl,
                   SUM(stake_usd) as total_staked,
                   AVG(edge_at_entry) as avg_edge,
                   AVG(evidence_quality) as avg_eq,
                   MAX(pnl) as best,
                   MIN(pnl) as worst
            FROM performance_log
            GROUP BY category
            ORDER BY SUM(pnl) DESC
        """)

        for r in rows:
            total = int(r["total"] or 0)
            wins = int(r["wins"] or 0)
            staked = float(r["total_staked"] or 0)
            pnl = float(r["total_pnl"] or 0)
            snap.category_stats.append(CategoryStats(
                category=r["category"] or "UNKNOWN",
                total_trades=total,
                wins=wins,
                losses=int(r["losses"] or 0),
                total_pnl=pnl,
                total_staked=staked,
                avg_edge=float(r["avg_edge"] or 0),
                avg_evidence_quality=float(r["avg_eq"] or 0),
                win_rate=wins / total if total > 0 else 0.0,
                roi_pct=(pnl / staked * 100) if staked > 0 else 0.0,
                best_trade_pnl=float(r["best"] or 0),
                worst_trade_pnl=float(r["worst"] or 0),
            ))

    # ── Calibration ──────────────────────────────────────────────────

    def _compute_calibration(
        self, conn: sqlite3.Connection, snap: PerformanceSnapshot
    ) -> None:
        rows = self._query_safe(conn, """
            SELECT forecast_prob, actual_outcome
            FROM calibration_history
            ORDER BY recorded_at ASC
        """)
        if not rows or len(rows) < 5:
            return

        n = len(rows)
        snap.calibration_samples = n
        brier_sum = 0.0
        for r in rows:
            fp = float(r["forecast_prob"])
            ao = float(r["actual_outcome"])
            brier_sum += (fp - ao) ** 2
        snap.brier_score = brier_sum / n

    # ── Equity Curve ─────────────────────────────────────────────────

    def _compute_equity_curve(
        self, conn: sqlite3.Connection, snap: PerformanceSnapshot
    ) -> None:
        rows = self._query_safe(conn, """
            SELECT date(resolved_at) as day, SUM(pnl) as daily_pnl,
                   COUNT(*) as trade_count
            FROM performance_log
            GROUP BY date(resolved_at)
            ORDER BY day ASC
        """)
        if not rows:
            return

        cum_pnl = 0.0
        peak_equity = self._bankroll
        max_dd = 0.0

        for r in rows:
            daily_pnl = float(r["daily_pnl"] or 0)
            cum_pnl += daily_pnl
            equity = self._bankroll + cum_pnl
            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            max_dd = max(max_dd, dd)

            snap.equity_curve.append(EquityPoint(
                timestamp=r["day"],
                equity=equity,
                pnl_cumulative=cum_pnl,
                drawdown_pct=dd,
                trade_count=int(r["trade_count"] or 0),
            ))

        snap.max_drawdown_pct = max_dd
        if max_dd > 0 and snap.total_pnl > 0:
            annualised_return = snap.roi_pct / 100
            snap.calmar_ratio = annualised_return / max_dd

    # ── Rolling Windows ──────────────────────────────────────────────

    def _compute_rolling_windows(
        self, conn: sqlite3.Connection, snap: PerformanceSnapshot
    ) -> None:
        for days, prefix in [(7, "7d"), (30, "30d")]:
            rows = self._query_safe(conn, f"""
                SELECT pnl, stake_usd
                FROM performance_log
                WHERE resolved_at >= datetime('now', '-{days} days')
            """)
            if not rows:
                continue

            pnls = [float(r["pnl"] or 0) for r in rows]
            total_pnl = sum(pnls)
            wins = sum(1 for p in pnls if p > 0)
            n = len(pnls)

            if prefix == "7d":
                snap.pnl_7d = total_pnl
                snap.win_rate_7d = wins / n if n > 0 else 0.0
                snap.trades_7d = n
            else:
                snap.pnl_30d = total_pnl
                snap.win_rate_30d = wins / n if n > 0 else 0.0
                snap.trades_30d = n

    # ── Model Accuracy ───────────────────────────────────────────────

    def _compute_model_accuracy(
        self, conn: sqlite3.Connection, snap: PerformanceSnapshot
    ) -> None:
        rows = self._query_safe(conn, """
            SELECT model_name, category,
                   COUNT(*) as total,
                   AVG(ABS(forecast_prob - actual_outcome)) as avg_error,
                   AVG((forecast_prob - actual_outcome) * (forecast_prob - actual_outcome)) as brier
            FROM model_forecast_log
            GROUP BY model_name, category
            ORDER BY model_name, category
        """)

        for r in rows:
            snap.model_accuracy.append(ModelAccuracy(
                model_name=r["model_name"] or "unknown",
                category=r["category"] or "ALL",
                total_forecasts=int(r["total"] or 0),
                avg_error=float(r["avg_error"] or 0),
                brier_score=float(r["brier"] or 0),
            ))

    # ── Open-Position Fallback (Paper Trading) ─────────────────────

    def _compute_from_open_positions(
        self, conn: sqlite3.Connection, snap: PerformanceSnapshot
    ) -> None:
        """Fallback analytics from open positions when performance_log is empty.

        This gives paper-trading users meaningful analytics even before any
        positions have resolved.  Metrics are based on *unrealised* P&L.
        """
        # Fetch positions joined with the most recent forecast per market
        rows = self._query_safe(conn, """
            SELECT p.market_id, p.direction, p.entry_price, p.current_price,
                   p.pnl, p.stake_usd, p.opened_at,
                   f.market_type, f.edge, f.evidence_quality,
                   f.model_probability, f.implied_probability
            FROM positions p
            LEFT JOIN forecasts f ON p.market_id = f.market_id
                AND f.created_at = (
                    SELECT MAX(f2.created_at) FROM forecasts f2
                    WHERE f2.market_id = p.market_id
                )
        """)

        if not rows:
            return

        # ── Trade-level metrics ──────────────────────────────────
        pnls: list[float] = []
        stakes: list[float] = []
        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        total_edge = 0.0
        current_streak = 0
        best_streak = 0
        worst_streak = 0

        # Category buckets
        cat_data: dict[str, list[dict[str, Any]]] = {}

        for r in rows:
            pnl = float(r["pnl"] or 0)
            stake = float(r["stake_usd"] or 0)
            edge = float(r["edge"] or 0)
            cat = r["market_type"] or "UNKNOWN"
            eq = float(r["evidence_quality"] or 0)

            pnls.append(pnl)
            stakes.append(stake)
            total_edge += edge

            if pnl > 0:
                wins += 1
                gross_profit += pnl
                current_streak = current_streak + 1 if current_streak >= 0 else 1
                best_streak = max(best_streak, current_streak)
            elif pnl < 0:
                losses += 1
                gross_loss += abs(pnl)
                current_streak = current_streak - 1 if current_streak <= 0 else -1
                worst_streak = min(worst_streak, current_streak)

            cat_data.setdefault(cat, []).append({
                "pnl": pnl, "stake": stake, "edge": edge, "eq": eq,
            })

        n = len(pnls)
        snap.total_trades = n
        snap.win_rate = wins / n if n > 0 else 0.0
        snap.total_pnl = sum(pnls)
        snap.total_staked = sum(stakes)
        snap.roi_pct = (
            (snap.total_pnl / snap.total_staked * 100) if snap.total_staked > 0 else 0.0
        )
        snap.profit_factor = (
            (gross_profit / gross_loss) if gross_loss > 0
            else (float("inf") if gross_profit > 0 else 0.0)
        )

        winning_pnls = [p for p in pnls if p > 0]
        losing_pnls = [p for p in pnls if p < 0]
        snap.avg_win = (
            sum(winning_pnls) / len(winning_pnls) if winning_pnls else 0.0
        )
        snap.avg_loss = (
            sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0.0
        )
        snap.largest_win = max(pnls) if pnls else 0.0
        snap.largest_loss = min(pnls) if pnls else 0.0
        snap.avg_edge_captured = total_edge / n if n > 0 else 0.0

        snap.current_streak = current_streak
        snap.best_streak = best_streak
        snap.worst_streak = worst_streak

        # Sharpe / Sortino (treat each position as one "return")
        if n >= 2:
            mean_ret = sum(pnls) / n
            std_ret = math.sqrt(
                sum((p - mean_ret) ** 2 for p in pnls) / (n - 1)
            )
            if std_ret > 0:
                snap.sharpe_ratio = mean_ret / std_ret * math.sqrt(252)
            downside = [p for p in pnls if p < 0]
            if downside:
                down_std = math.sqrt(
                    sum(p ** 2 for p in downside) / len(downside)
                )
                if down_std > 0:
                    snap.sortino_ratio = mean_ret / down_std * math.sqrt(252)

        # ── Category breakdown ───────────────────────────────────
        for cat, items in cat_data.items():
            cat_pnls = [it["pnl"] for it in items]
            cat_stakes = [it["stake"] for it in items]
            cat_wins = sum(1 for p in cat_pnls if p > 0)
            cat_total = len(items)
            cat_total_pnl = sum(cat_pnls)
            cat_total_staked = sum(cat_stakes)

            snap.category_stats.append(CategoryStats(
                category=cat,
                total_trades=cat_total,
                wins=cat_wins,
                losses=sum(1 for p in cat_pnls if p < 0),
                total_pnl=cat_total_pnl,
                total_staked=cat_total_staked,
                avg_edge=sum(it["edge"] for it in items) / cat_total if cat_total else 0.0,
                avg_evidence_quality=sum(it["eq"] for it in items) / cat_total if cat_total else 0.0,
                win_rate=cat_wins / cat_total if cat_total > 0 else 0.0,
                roi_pct=(
                    (cat_total_pnl / cat_total_staked * 100)
                    if cat_total_staked > 0 else 0.0
                ),
                best_trade_pnl=max(cat_pnls) if cat_pnls else 0.0,
                worst_trade_pnl=min(cat_pnls) if cat_pnls else 0.0,
            ))

        # ── Equity curve from trades + positions ─────────────────
        trade_rows = self._query_safe(conn, """
            SELECT t.market_id, t.created_at, t.stake_usd, p.pnl, p.size
            FROM trades t
            JOIN positions p ON t.market_id = p.market_id
            WHERE t.status IN ('DRY_RUN', 'FILLED')
            ORDER BY t.created_at ASC
        """)
        if trade_rows:
            # Distribute each position's P&L evenly across its trades
            from collections import Counter
            trade_counts: Counter[str] = Counter()
            for tr in trade_rows:
                trade_counts[tr["market_id"]] += 1

            cum_pnl = 0.0
            peak_equity = self._bankroll
            max_dd = 0.0
            seen_markets: dict[str, int] = {}

            for tr in trade_rows:
                mid = tr["market_id"]
                pos_pnl = float(tr["pnl"] or 0)
                count = trade_counts[mid]
                share = pos_pnl / count if count > 0 else 0.0

                seen_markets[mid] = seen_markets.get(mid, 0) + 1
                cum_pnl += share
                equity = self._bankroll + cum_pnl
                peak_equity = max(peak_equity, equity)
                dd = (
                    (peak_equity - equity) / peak_equity
                    if peak_equity > 0 else 0.0
                )
                max_dd = max(max_dd, dd)

                snap.equity_curve.append(EquityPoint(
                    timestamp=tr["created_at"],
                    equity=equity,
                    pnl_cumulative=cum_pnl,
                    drawdown_pct=dd,
                    trade_count=seen_markets[mid],
                ))

            snap.max_drawdown_pct = max_dd
            if max_dd > 0 and snap.total_pnl > 0:
                annualised_return = snap.roi_pct / 100
                snap.calmar_ratio = annualised_return / max_dd

        # ── Rolling windows (by opened_at) ───────────────────────
        for days, prefix in [(7, "7d"), (30, "30d")]:
            window_rows = self._query_safe(conn, f"""
                SELECT pnl, stake_usd
                FROM positions
                WHERE opened_at >= datetime('now', '-{days} days')
            """)
            if not window_rows:
                continue
            w_pnls = [float(r["pnl"] or 0) for r in window_rows]
            w_wins = sum(1 for p in w_pnls if p > 0)
            w_n = len(w_pnls)
            if prefix == "7d":
                snap.pnl_7d = sum(w_pnls)
                snap.win_rate_7d = w_wins / w_n if w_n > 0 else 0.0
                snap.trades_7d = w_n
            else:
                snap.pnl_30d = sum(w_pnls)
                snap.win_rate_30d = w_wins / w_n if w_n > 0 else 0.0
                snap.trades_30d = w_n

        log.info(
            "performance_tracker.open_position_fallback",
            positions=n, total_pnl=round(snap.total_pnl, 2),
            categories=len(snap.category_stats),
        )

    # ── Leaderboard ──────────────────────────────────────────────────

    def _build_leaderboard(self, snap: PerformanceSnapshot) -> None:
        """Build strategy leaderboard from category stats, ranked by ROI."""
        snap.leaderboard = sorted(
            [
                {
                    "rank": 0,
                    "category": cs.category,
                    "roi_pct": cs.roi_pct,
                    "win_rate": cs.win_rate,
                    "total_pnl": cs.total_pnl,
                    "trades": cs.total_trades,
                    "avg_edge": cs.avg_edge,
                    "score": (
                        cs.roi_pct * 0.4
                        + cs.win_rate * 100 * 0.3
                        + min(cs.total_trades / 10, 1) * 30 * 0.3
                    ),
                }
                for cs in snap.category_stats
                if cs.total_trades >= 1
            ],
            key=lambda x: x["score"],
            reverse=True,
        )
        for i, entry in enumerate(snap.leaderboard):
            entry["rank"] = i + 1

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _query_safe(
        conn: sqlite3.Connection, sql: str, params: tuple = ()
    ) -> list[sqlite3.Row]:
        """Run a query, returning empty list if table doesn't exist."""
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

"""Market regime detection — adapt strategy to market conditions.

Detects three regimes:
  1. TRENDING — Markets are moving directionally, favor momentum
  2. MEAN_REVERTING — Markets oscillate, favor contrarian entries
  3. HIGH_VOLATILITY — Markets are chaotic, reduce position sizes

Signals used:
  - Price momentum (short-term vs long-term)
  - Volatility (realized vs implied)
  - Win/loss streaks (recent bot performance)
  - Market-wide volume patterns
  - Prediction market spread distributions

The regime affects:
  - Kelly fraction multiplier (reduce in high vol)
  - Edge threshold (raise in high vol)
  - Position sizing (reduce in volatile/uncertain regimes)
  - Entry aggressiveness (more patient in mean-reverting)
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.observability.logger import get_logger

log = get_logger(__name__)


class Regime:
    """Market regime constants."""
    NORMAL = "NORMAL"
    TRENDING = "TRENDING"
    MEAN_REVERTING = "MEAN_REVERTING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_ACTIVITY = "LOW_ACTIVITY"


@dataclass
class RegimeSignals:
    """Raw signals used for regime detection."""
    # Price-based
    avg_price_momentum: float = 0.0      # average |price change| across markets
    momentum_direction_bias: float = 0.0  # -1 (bearish) to +1 (bullish)
    price_volatility: float = 0.0        # std dev of price changes

    # Bot performance
    recent_win_rate: float = 0.5
    current_streak: int = 0
    recent_avg_pnl: float = 0.0
    recent_trade_count: int = 0

    # Market-wide
    avg_spread: float = 0.0
    avg_volume: float = 0.0
    markets_active: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {k: round(v, 4) if isinstance(v, float) else v
                for k, v in self.__dict__.items()}


@dataclass
class RegimeState:
    """Current detected market regime and adjustments."""
    regime: str = Regime.NORMAL
    confidence: float = 0.5
    signals: RegimeSignals = field(default_factory=RegimeSignals)

    # Multipliers applied to strategy parameters
    kelly_multiplier: float = 1.0       # multiply Kelly fraction
    edge_threshold_multiplier: float = 1.0  # multiply min_edge
    size_multiplier: float = 1.0        # multiply position sizes
    entry_patience: float = 1.0         # 1.0 = normal, >1 = more patient

    # Human-readable explanation
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "confidence": round(self.confidence, 3),
            "signals": self.signals.to_dict(),
            "kelly_multiplier": round(self.kelly_multiplier, 3),
            "edge_threshold_multiplier": round(self.edge_threshold_multiplier, 3),
            "size_multiplier": round(self.size_multiplier, 3),
            "entry_patience": round(self.entry_patience, 3),
            "explanation": self.explanation,
        }


class RegimeDetector:
    """Detect market regime from recent data and adjust strategy."""

    def __init__(
        self,
        lookback_trades: int = 20,
        vol_high_threshold: float = 0.15,
        vol_low_threshold: float = 0.03,
        momentum_threshold: float = 0.08,
        min_trades_for_signal: int = 5,
    ):
        self._lookback = lookback_trades
        self._vol_high = vol_high_threshold
        self._vol_low = vol_low_threshold
        self._momentum_thresh = momentum_threshold
        self._min_trades = min_trades_for_signal

    def detect(self, conn: sqlite3.Connection) -> RegimeState:
        """Detect current regime from DB data."""
        signals = self._gather_signals(conn)
        regime, confidence, explanation = self._classify_regime(signals)
        multipliers = self._compute_multipliers(regime, confidence, signals)

        state = RegimeState(
            regime=regime,
            confidence=confidence,
            signals=signals,
            explanation=explanation,
            **multipliers,
        )

        log.info(
            "regime.detected",
            regime=regime,
            confidence=round(confidence, 3),
            kelly_mult=round(state.kelly_multiplier, 3),
            size_mult=round(state.size_multiplier, 3),
        )

        return state

    def _gather_signals(self, conn: sqlite3.Connection) -> RegimeSignals:
        """Gather signal data from the database."""
        signals = RegimeSignals()

        # Bot performance (from performance_log)
        try:
            rows = conn.execute(f"""
                SELECT pnl, edge_at_entry
                FROM performance_log
                ORDER BY resolved_at DESC
                LIMIT {self._lookback}
            """).fetchall()

            if rows:
                pnls = [float(r["pnl"] or 0) for r in rows]
                signals.recent_trade_count = len(pnls)
                signals.recent_avg_pnl = sum(pnls) / len(pnls)
                wins = sum(1 for p in pnls if p > 0)
                signals.recent_win_rate = wins / len(pnls) if pnls else 0.5

                # Streak detection
                streak = 0
                for p in pnls:
                    if p > 0:
                        if streak >= 0:
                            streak += 1
                        else:
                            break
                    elif p < 0:
                        if streak <= 0:
                            streak -= 1
                        else:
                            break
                signals.current_streak = streak

                # PnL volatility as a proxy for market regime
                if len(pnls) >= 2:
                    mean_pnl = sum(pnls) / len(pnls)
                    var = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
                    signals.price_volatility = math.sqrt(var)

        except sqlite3.OperationalError:
            pass

        # Market-wide signals (from recent candidates)
        try:
            rows = conn.execute("""
                SELECT edge, implied_prob
                FROM candidates
                ORDER BY created_at DESC
                LIMIT 50
            """).fetchall()

            if rows:
                edges = [float(r["edge"] or 0) for r in rows]
                probs = [float(r["implied_prob"] or 0.5) for r in rows]
                signals.markets_active = len(rows)

                # Price momentum = average absolute edge
                signals.avg_price_momentum = sum(abs(e) for e in edges) / len(edges)

                # Direction bias = average signed edge
                signals.momentum_direction_bias = sum(edges) / len(edges)

                # Spread distribution = how clustered are market prices
                if len(probs) >= 2:
                    mean_p = sum(probs) / len(probs)
                    spread_var = sum((p - mean_p) ** 2 for p in probs) / (len(probs) - 1)
                    signals.avg_spread = math.sqrt(spread_var)

        except sqlite3.OperationalError:
            pass

        return signals

    def _classify_regime(
        self, signals: RegimeSignals
    ) -> tuple[str, float, str]:
        """Classify regime based on signals."""
        # Not enough data — default to NORMAL
        if signals.recent_trade_count < self._min_trades:
            return (
                Regime.NORMAL,
                0.3,
                "Insufficient data for regime detection — using defaults",
            )

        scores: dict[str, float] = {
            Regime.NORMAL: 0.3,
            Regime.TRENDING: 0.0,
            Regime.MEAN_REVERTING: 0.0,
            Regime.HIGH_VOLATILITY: 0.0,
            Regime.LOW_ACTIVITY: 0.0,
        }

        # High volatility signals
        if signals.price_volatility > self._vol_high:
            scores[Regime.HIGH_VOLATILITY] += 0.4
        if abs(signals.current_streak) >= 3:
            # Strong streaks suggest volatility
            scores[Regime.HIGH_VOLATILITY] += 0.2

        # Trending signals
        if abs(signals.momentum_direction_bias) > self._momentum_thresh:
            scores[Regime.TRENDING] += 0.4
        if signals.recent_win_rate > 0.65:
            # Winning consistently suggests we're in a good trend
            scores[Regime.TRENDING] += 0.2

        # Mean-reverting signals
        if (signals.price_volatility < self._vol_low
                and signals.avg_price_momentum > 0.02):
            scores[Regime.MEAN_REVERTING] += 0.3
        if 0.4 <= signals.recent_win_rate <= 0.6:
            scores[Regime.MEAN_REVERTING] += 0.2

        # Low activity
        if signals.markets_active < 5:
            scores[Regime.LOW_ACTIVITY] += 0.3
        if signals.recent_trade_count < self._min_trades:
            scores[Regime.LOW_ACTIVITY] += 0.2

        # Pick highest-scoring regime
        best_regime = max(scores, key=lambda k: scores[k])
        best_score = scores[best_regime]

        # Confidence = how much better than second-best
        sorted_scores = sorted(scores.values(), reverse=True)
        margin = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else 0
        confidence = min(1.0, best_score + margin)

        explanations = {
            Regime.NORMAL: "Markets operating normally — standard strategy applies",
            Regime.TRENDING: f"Directional trend detected (bias: {signals.momentum_direction_bias:+.3f}) — lean into momentum",
            Regime.MEAN_REVERTING: "Low volatility with price oscillation — contrarian entries favored",
            Regime.HIGH_VOLATILITY: f"High volatility detected (σ={signals.price_volatility:.3f}) — reducing exposure",
            Regime.LOW_ACTIVITY: "Low market activity — fewer opportunities available",
        }

        return best_regime, confidence, explanations[best_regime]

    def _compute_multipliers(
        self,
        regime: str,
        confidence: float,
        signals: RegimeSignals,
    ) -> dict[str, float]:
        """Compute strategy multipliers based on regime."""
        # Defaults for NORMAL regime
        multipliers = {
            "kelly_multiplier": 1.0,
            "edge_threshold_multiplier": 1.0,
            "size_multiplier": 1.0,
            "entry_patience": 1.0,
        }

        # Scale adjustments by confidence
        c = confidence

        if regime == Regime.HIGH_VOLATILITY:
            multipliers["kelly_multiplier"] = 1.0 - 0.40 * c      # reduce Kelly by up to 40%
            multipliers["edge_threshold_multiplier"] = 1.0 + 0.50 * c  # require 50% more edge
            multipliers["size_multiplier"] = 1.0 - 0.30 * c       # reduce sizes by 30%
            multipliers["entry_patience"] = 1.0 + 0.50 * c        # be 50% more patient

        elif regime == Regime.TRENDING:
            multipliers["kelly_multiplier"] = 1.0 + 0.15 * c      # slightly increase Kelly
            multipliers["edge_threshold_multiplier"] = 1.0 - 0.10 * c  # slightly lower threshold
            multipliers["size_multiplier"] = 1.0 + 0.10 * c       # slightly larger positions
            multipliers["entry_patience"] = 1.0 - 0.20 * c        # more aggressive entry

        elif regime == Regime.MEAN_REVERTING:
            multipliers["kelly_multiplier"] = 1.0
            multipliers["edge_threshold_multiplier"] = 1.0
            multipliers["size_multiplier"] = 1.0
            multipliers["entry_patience"] = 1.0 + 0.30 * c        # more patient entries

        elif regime == Regime.LOW_ACTIVITY:
            multipliers["kelly_multiplier"] = 1.0 - 0.20 * c
            multipliers["edge_threshold_multiplier"] = 1.0 + 0.30 * c
            multipliers["size_multiplier"] = 1.0 - 0.20 * c
            multipliers["entry_patience"] = 1.0 + 0.40 * c

        return multipliers

"""Calibrator — adjusts raw LLM probabilities for better calibration.

Supports:
  - Platt scaling (logistic compression with configurable shrinkage)
  - Historical calibration (learn from own forecast vs outcome history)
  - Isotonic regression (non-parametric calibration)
  - No-op pass-through

Heuristic adjustments:
  1. Shrinks extreme probabilities toward 0.5
  2. Penalizes low-evidence forecasts
  3. Applies contradiction penalties
  4. Ensemble disagreement penalty
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class CalibrationResult:
    raw_probability: float
    calibrated_probability: float
    method: str
    adjustments: list[str] = field(default_factory=list)


@dataclass
class CalibrationHistory:
    """Stored (forecast, outcome) pair for learning calibration."""
    forecast_prob: float
    actual_outcome: float  # 0.0 or 1.0
    market_type: str = ""
    confidence_level: str = ""
    timestamp: str = ""


class HistoricalCalibrator:
    """Learn calibration from historical forecasts vs outcomes.

    Uses logistic regression to find optimal a, b such that:
      calibrated = sigmoid(a * logit(raw) + b)

    Falls back to heuristic if insufficient history.
    """

    def __init__(self, min_samples: int = 30):
        self._min_samples = min_samples
        self._a: float = 1.0  # slope
        self._b: float = 0.0  # intercept
        self._is_fitted: bool = False
        self._n_samples: int = 0
        self._brier_score: float = 1.0

    def fit(self, history: list[CalibrationHistory]) -> bool:
        """Fit calibration from historical data. Returns True if successful."""
        if len(history) < self._min_samples:
            log.info(
                "calibrator.insufficient_history",
                samples=len(history),
                required=self._min_samples,
            )
            return False

        try:
            from sklearn.linear_model import LogisticRegression
            import numpy as np

            # Convert forecasts to logits
            probs = [max(0.01, min(0.99, h.forecast_prob)) for h in history]
            logits = [math.log(p / (1 - p)) for p in probs]
            outcomes = [h.actual_outcome for h in history]

            X = np.array(logits).reshape(-1, 1)
            y = np.array(outcomes)

            lr = LogisticRegression(solver="lbfgs", max_iter=1000)
            lr.fit(X, y)

            self._a = float(lr.coef_[0][0])
            self._b = float(lr.intercept_[0])
            self._is_fitted = True
            self._n_samples = len(history)

            # Compute Brier score
            calibrated = [self._apply(p) for p in probs]
            self._brier_score = sum(
                (c - o) ** 2 for c, o in zip(calibrated, outcomes)
            ) / len(outcomes)

            log.info(
                "calibrator.fitted",
                a=round(self._a, 4),
                b=round(self._b, 4),
                samples=self._n_samples,
                brier=round(self._brier_score, 4),
            )
            return True

        except ImportError:
            log.warning("calibrator.sklearn_not_available")
            return False
        except Exception as e:
            log.error("calibrator.fit_failed", error=str(e))
            return False

    def _apply(self, prob: float) -> float:
        """Apply learned calibration."""
        prob = max(0.01, min(0.99, prob))
        logit = math.log(prob / (1 - prob))
        cal_logit = self._a * logit + self._b
        return 1.0 / (1.0 + math.exp(-cal_logit))

    def calibrate(self, prob: float) -> float:
        """Calibrate using learned model if fitted, else identity."""
        if self._is_fitted:
            return max(0.01, min(0.99, self._apply(prob)))
        return prob

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "is_fitted": self._is_fitted,
            "n_samples": self._n_samples,
            "a": round(self._a, 4),
            "b": round(self._b, 4),
            "brier_score": round(self._brier_score, 4),
        }


# Global historical calibrator instance
_historical = HistoricalCalibrator()


def get_historical_calibrator() -> HistoricalCalibrator:
    """Get the global historical calibrator."""
    return _historical


def calibrate(
    raw_prob: float,
    evidence_quality: float,
    num_contradictions: int = 0,
    method: str = "platt",
    low_evidence_penalty: float = 0.15,
    ensemble_spread: float = 0.0,
) -> CalibrationResult:
    """Calibrate a raw probability estimate.

    Methods:
      - "historical": Use learned calibration (if available, falls back to platt)
      - "platt": Heuristic logistic compression
      - "none": No calibration

    Additional adjustments:
      1. Shrink extremes (nothing is truly 0 or 1)
      2. Pull toward 0.5 when evidence is weak
      3. Pull toward 0.5 for each contradiction
      4. Pull toward 0.5 if ensemble models disagree (high spread)
    """
    adjustments: list[str] = []
    p = max(0.01, min(0.99, raw_prob))

    if method == "none":
        return CalibrationResult(
            raw_probability=raw_prob,
            calibrated_probability=p,
            method="none",
            adjustments=[],
        )

    # Try historical calibration first
    if method == "historical" and _historical.is_fitted:
        p_hist = _historical.calibrate(p)
        if abs(p_hist - p) > 0.005:
            adjustments.append(f"historical_cal: {p:.3f} → {p_hist:.3f}")
            p = p_hist
        method_used = "historical"
    elif method in ("platt", "historical"):
        # Platt-like: apply logistic compression
        logit = math.log(p / (1 - p))
        shrunk = logit * 0.90  # 10% shrinkage toward 0
        p_shrunk = 1.0 / (1.0 + math.exp(-shrunk))
        if abs(p_shrunk - p) > 0.005:
            adjustments.append(f"extremity_shrinkage: {p:.3f} → {p_shrunk:.3f}")
            p = p_shrunk
        method_used = "platt"
    else:
        method_used = method

    # 2. Low evidence penalty
    if evidence_quality < 0.4:
        penalty_weight = low_evidence_penalty * (1.0 - evidence_quality)
        p_penalized = p * (1 - penalty_weight) + 0.5 * penalty_weight
        adjustments.append(
            f"low_evidence_penalty (q={evidence_quality:.2f}): {p:.3f} → {p_penalized:.3f}"
        )
        p = p_penalized

    # 3. Contradiction penalty
    if num_contradictions > 0:
        contradiction_weight = min(0.3, 0.1 * num_contradictions)
        p_contra = p * (1 - contradiction_weight) + 0.5 * contradiction_weight
        adjustments.append(
            f"contradiction_penalty (n={num_contradictions}): {p:.3f} → {p_contra:.3f}"
        )
        p = p_contra

    # 4. Ensemble disagreement penalty
    if ensemble_spread > 0.10:
        spread_weight = min(0.25, ensemble_spread)
        p_spread = p * (1 - spread_weight) + 0.5 * spread_weight
        adjustments.append(
            f"ensemble_spread_penalty (s={ensemble_spread:.2f}): {p:.3f} → {p_spread:.3f}"
        )
        p = p_spread

    p = max(0.01, min(0.99, p))

    log.info(
        "calibrator.calibrated",
        raw=round(raw_prob, 4),
        calibrated=round(p, 4),
        method=method_used,
        adjustments=len(adjustments),
    )

    return CalibrationResult(
        raw_probability=raw_prob,
        calibrated_probability=p,
        method=method_used,
        adjustments=adjustments,
    )

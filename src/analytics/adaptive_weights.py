"""Adaptive model weighting — learn which LLMs are best per category.

Instead of static weights (GPT-4o: 40%, Claude: 35%, Gemini: 25%),
this module dynamically reweights ensemble models based on their
historical accuracy for each market category.

Key features:
  - Tracks per-model, per-category Brier scores
  - Computes inverse-Brier weights (better models get more weight)
  - Falls back to default weights when insufficient data
  - Blends learned weights with priors using Bayesian smoothing
  - Provides confidence in learned weights based on sample size
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.config import EnsembleConfig
from src.observability.logger import get_logger

log = get_logger(__name__)

# Minimum samples before we trust learned weights for a category
MIN_SAMPLES_PER_MODEL = 5
# Blend factor: how much to trust learned weights vs priors
# 0 = all prior, 1 = all learned; scales with sample size
BLEND_FULL_CONFIDENCE_SAMPLES = 50


@dataclass
class ModelWeight:
    """Weight for a single model in a specific context."""
    model_name: str
    weight: float
    source: str  # "learned" | "default" | "blended"
    brier_score: float = 0.0
    sample_count: int = 0
    confidence: float = 0.0  # 0-1, how confident we are in this weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "weight": round(self.weight, 4),
            "source": self.source,
            "brier_score": round(self.brier_score, 4),
            "sample_count": self.sample_count,
            "confidence": round(self.confidence, 3),
        }


@dataclass
class AdaptiveWeightResult:
    """Result of adaptive weight computation."""
    category: str
    weights: dict[str, float]
    details: list[ModelWeight] = field(default_factory=list)
    data_available: bool = False
    blend_factor: float = 0.0  # 0 = default, 1 = fully learned

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "details": [d.to_dict() for d in self.details],
            "data_available": self.data_available,
            "blend_factor": round(self.blend_factor, 3),
        }


class AdaptiveModelWeighter:
    """Computes dynamic model weights based on historical performance."""

    def __init__(self, ensemble_config: EnsembleConfig):
        self._config = ensemble_config
        self._default_weights = dict(ensemble_config.weights)
        self._models = list(ensemble_config.models)

    def get_weights(
        self,
        conn: sqlite3.Connection,
        category: str,
    ) -> AdaptiveWeightResult:
        """Get adaptive weights for models in the given category.

        Strategy:
          1. Query per-model, per-category Brier scores
          2. If enough data, compute inverse-Brier weights
          3. Blend learned weights with default priors based on sample size
          4. If no data, return default weights
        """
        # Try to get learned weights
        learned = self._get_learned_weights(conn, category)

        if not learned:
            # No data at all — use defaults
            details = [
                ModelWeight(
                    model_name=m,
                    weight=self._default_weights.get(m, 1.0 / len(self._models)),
                    source="default",
                )
                for m in self._models
            ]
            return AdaptiveWeightResult(
                category=category,
                weights=dict(self._default_weights),
                details=details,
                data_available=False,
                blend_factor=0.0,
            )

        # Compute blend factor based on minimum sample count
        min_samples = min(lw.sample_count for lw in learned.values())
        blend = min(1.0, min_samples / BLEND_FULL_CONFIDENCE_SAMPLES)

        # Blend learned weights with defaults
        final_weights: dict[str, float] = {}
        details: list[ModelWeight] = []

        for model in self._models:
            default_w = self._default_weights.get(model, 1.0 / len(self._models))

            if model in learned:
                lw = learned[model]
                blended = blend * lw.weight + (1 - blend) * default_w
                final_weights[model] = blended
                details.append(ModelWeight(
                    model_name=model,
                    weight=blended,
                    source="blended" if blend < 0.95 else "learned",
                    brier_score=lw.brier_score,
                    sample_count=lw.sample_count,
                    confidence=blend,
                ))
            else:
                final_weights[model] = default_w
                details.append(ModelWeight(
                    model_name=model,
                    weight=default_w,
                    source="default",
                ))

        # Re-normalize to sum to 1.0
        total = sum(final_weights.values())
        if total > 0:
            final_weights = {k: v / total for k, v in final_weights.items()}
            for d in details:
                d.weight = final_weights.get(d.model_name, d.weight)

        log.info(
            "adaptive_weights.computed",
            category=category,
            blend=round(blend, 3),
            weights={k: round(v, 3) for k, v in final_weights.items()},
        )

        return AdaptiveWeightResult(
            category=category,
            weights=final_weights,
            details=details,
            data_available=True,
            blend_factor=blend,
        )

    def get_all_category_weights(
        self, conn: sqlite3.Connection
    ) -> dict[str, AdaptiveWeightResult]:
        """Get weights for all categories that have data."""
        try:
            rows = conn.execute("""
                SELECT DISTINCT category FROM model_forecast_log
            """).fetchall()
        except sqlite3.OperationalError:
            return {}

        results: dict[str, AdaptiveWeightResult] = {}
        for r in rows:
            cat = r["category"] or "UNKNOWN"
            results[cat] = self.get_weights(conn, cat)

        # Also include ALL-categories aggregate
        results["ALL"] = self.get_weights(conn, "ALL")
        return results

    # ── Internal ─────────────────────────────────────────────────────

    def _get_learned_weights(
        self, conn: sqlite3.Connection, category: str
    ) -> dict[str, ModelWeight]:
        """Query DB for per-model Brier scores and compute weights."""
        try:
            if category == "ALL":
                rows = conn.execute("""
                    SELECT model_name,
                           AVG((forecast_prob - actual_outcome) *
                               (forecast_prob - actual_outcome)) as brier,
                           COUNT(*) as cnt
                    FROM model_forecast_log
                    GROUP BY model_name
                    HAVING cnt >= ?
                """, (MIN_SAMPLES_PER_MODEL,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT model_name,
                           AVG((forecast_prob - actual_outcome) *
                               (forecast_prob - actual_outcome)) as brier,
                           COUNT(*) as cnt
                    FROM model_forecast_log
                    WHERE category = ?
                    GROUP BY model_name
                    HAVING cnt >= ?
                """, (category, MIN_SAMPLES_PER_MODEL)).fetchall()
        except sqlite3.OperationalError:
            return {}

        if not rows:
            return {}

        # Inverse-Brier weighting
        raw_weights: dict[str, ModelWeight] = {}
        for r in rows:
            brier = float(r["brier"])
            cnt = int(r["cnt"])
            inv_brier = 1.0 / max(brier, 0.001)
            raw_weights[r["model_name"]] = ModelWeight(
                model_name=r["model_name"],
                weight=inv_brier,
                source="learned",
                brier_score=brier,
                sample_count=cnt,
                confidence=min(1.0, cnt / BLEND_FULL_CONFIDENCE_SAMPLES),
            )

        # Normalize to sum to 1.0
        total = sum(mw.weight for mw in raw_weights.values())
        if total > 0:
            for mw in raw_weights.values():
                mw.weight /= total

        return raw_weights

"""Timeline intelligence — resolution timing, urgency scoring, exit triggers.

Markets near resolution behave differently:
  - Convergence to true price accelerates
  - Remaining edge gets captured faster
  - But liquidity can dry up
  - And slippage risk increases

This module scores markets by resolution urgency and provides
timing-aware adjustments for sizing and execution.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from src.config import load_config
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class TimelineAssessment:
    """Assessment of a market's resolution timeline."""
    market_id: str
    hours_to_resolution: float
    days_to_resolution: float
    urgency_score: float  # 0-1, higher = more urgent
    phase: str  # "early", "mid", "late", "endgame"
    sizing_multiplier: float  # applied to base position size
    should_exit_before: bool  # True if too close to resolution for new entries
    exit_deadline_hours: float  # hours before resolution to exit

    # Edge adjustments
    edge_confidence_boost: float  # boost for near-resolution certainty
    liquidity_risk_penalty: float  # penalty for endgame illiquidity

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


def assess_timeline(
    market_id: str,
    end_date: dt.datetime | None,
    config: Any | None = None,
) -> TimelineAssessment:
    """Assess a market's resolution timeline and produce timing adjustments."""
    cfg = config or load_config()
    tl = cfg.timeline

    # Default assessment for markets with no end date
    if end_date is None:
        return TimelineAssessment(
            market_id=market_id,
            hours_to_resolution=float("inf"),
            days_to_resolution=float("inf"),
            urgency_score=0.0,
            phase="unknown",
            sizing_multiplier=1.0,
            should_exit_before=False,
            exit_deadline_hours=tl.exit_before_resolution_hours,
            edge_confidence_boost=0.0,
            liquidity_risk_penalty=0.0,
        )

    now = dt.datetime.now(dt.timezone.utc)
    delta = end_date - now
    hours_left = max(delta.total_seconds() / 3600, 0)
    days_left = hours_left / 24

    # Determine phase
    if days_left > tl.early_market_days_threshold:
        phase = "early"
    elif days_left > tl.time_decay_urgency_start_days:
        phase = "mid"
    elif hours_left > tl.exit_before_resolution_hours:
        phase = "late"
    else:
        phase = "endgame"

    # Urgency score (0-1)
    if days_left <= 1:
        urgency = 1.0
    elif days_left <= tl.time_decay_urgency_start_days:
        urgency = 0.5 + 0.5 * (1 - days_left / tl.time_decay_urgency_start_days)
    elif days_left <= tl.early_market_days_threshold:
        urgency = 0.2 + 0.3 * (1 - days_left / tl.early_market_days_threshold)
    else:
        urgency = max(0.0, 0.2 - days_left / 365)

    # Sizing multiplier
    if phase == "early":
        # Slightly penalise very early markets (less info, more uncertainty)
        sizing_mult = 0.8
    elif phase == "mid":
        sizing_mult = 1.0
    elif phase == "late":
        # Boost for near-resolution (edge converges faster)
        sizing_mult = min(1.3, 1.0 + urgency * 0.5)
    else:  # endgame
        # Reduce sizing — liquidity risk
        sizing_mult = 0.5

    # Should exit before resolution?
    should_exit = hours_left <= tl.exit_before_resolution_hours

    # Edge confidence boost (market converges to truth near resolution)
    edge_boost = 0.0
    if phase == "late":
        edge_boost = min(0.15, urgency * 0.2)
    elif phase == "endgame":
        edge_boost = 0.0  # too risky, no boost

    # Liquidity risk penalty
    liq_penalty = 0.0
    if phase == "endgame":
        liq_penalty = 0.3
    elif phase == "late" and days_left < 2:
        liq_penalty = 0.1

    assessment = TimelineAssessment(
        market_id=market_id,
        hours_to_resolution=hours_left,
        days_to_resolution=days_left,
        urgency_score=urgency,
        phase=phase,
        sizing_multiplier=sizing_mult,
        should_exit_before=should_exit,
        exit_deadline_hours=tl.exit_before_resolution_hours,
        edge_confidence_boost=edge_boost,
        liquidity_risk_penalty=liq_penalty,
    )

    log.info(
        "timeline.assessed",
        market_id=market_id,
        days_left=round(days_left, 1),
        phase=phase,
        urgency=round(urgency, 2),
        sizing_mult=round(sizing_mult, 2),
    )
    return assessment

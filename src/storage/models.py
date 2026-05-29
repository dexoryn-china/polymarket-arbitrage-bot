"""Database models — Pydantic models for storage records."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class MarketRecord(BaseModel):
    """Stored market data."""
    id: str
    condition_id: str = ""
    question: str = ""
    market_type: str = ""
    category: str = ""
    volume: float = 0.0
    liquidity: float = 0.0
    end_date: str = ""
    resolution_source: str = ""
    first_seen: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )
    last_updated: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )


class ForecastRecord(BaseModel):
    """Stored forecast."""
    id: str = ""
    market_id: str
    question: str = ""
    market_type: str = ""
    implied_probability: float = 0.5
    model_probability: float = 0.5
    edge: float = 0.0
    confidence_level: str = "LOW"
    evidence_quality: float = 0.0
    num_sources: int = 0
    decision: str = "NO TRADE"
    reasoning: str = ""
    evidence_json: str = "[]"
    invalidation_triggers_json: str = "[]"
    research_evidence_json: str = "{}"
    created_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )


class TradeRecord(BaseModel):
    """Stored trade."""
    id: str = ""
    order_id: str
    market_id: str
    token_id: str = ""
    side: str = ""
    price: float = 0.0
    size: float = 0.0
    stake_usd: float = 0.0
    status: str = ""
    dry_run: bool = True
    created_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )


class PositionRecord(BaseModel):
    """Tracked open position."""
    market_id: str
    token_id: str = ""
    direction: str = ""
    entry_price: float = 0.0
    size: float = 0.0
    stake_usd: float = 0.0
    current_price: float = 0.0
    pnl: float = 0.0
    opened_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )
    question: str = ""
    market_type: str = ""


class ClosedPositionRecord(BaseModel):
    """Archived closed position with full context."""
    id: str = ""
    market_id: str
    token_id: str = ""
    direction: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    size: float = 0.0
    stake_usd: float = 0.0
    pnl: float = 0.0
    close_reason: str = ""
    question: str = ""
    market_type: str = ""
    opened_at: str = ""
    closed_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )


class PerformanceLogRecord(BaseModel):
    """Record for the performance_log table — one per resolved/closed trade."""
    market_id: str
    question: str = ""
    category: str = "UNKNOWN"
    forecast_prob: float = 0.0
    actual_outcome: float | None = None
    edge_at_entry: float = 0.0
    confidence: str = "LOW"
    evidence_quality: float = 0.0
    stake_usd: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    pnl: float = 0.0
    holding_hours: float = 0.0
    resolved_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )


class RegimeHistoryRecord(BaseModel):
    """Detected market regime snapshot."""
    timestamp: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )
    regime: str = "normal"  # normal | volatile | trending | mean_reverting
    confidence: float = 0.0
    volatility_30d: float = 0.0
    trend_strength: float = 0.0
    metadata_json: str = "{}"


class ModelForecastLogRecord(BaseModel):
    """Individual model forecast within an ensemble run."""
    market_id: str
    model_name: str
    model_probability: float = 0.5
    confidence_level: str = "LOW"
    reasoning: str = ""
    latency_ms: float = 0.0
    error: str = ""
    created_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )


class CandidateRecord(BaseModel):
    """Market candidate discovered during scan, before research."""
    market_id: str
    question: str = ""
    market_type: str = ""
    score: float = 0.0
    volume: float = 0.0
    liquidity: float = 0.0
    implied_probability: float = 0.5
    spread: float = 0.0
    status: str = "pending"  # pending | researching | forecasted | traded | skipped
    discovered_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )


class AlertRecord(BaseModel):
    """Persisted alert for audit trail."""
    level: str = "info"
    title: str = ""
    message: str = ""
    channels_sent: str = "[]"  # JSON list
    data_json: str = "{}"
    created_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )


class CalibrationHistoryRecord(BaseModel):
    """Calibration model training snapshot."""
    num_samples: int = 0
    brier_score: float = 0.0
    calibration_error: float = 0.0
    model_params_json: str = "{}"
    trained_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )

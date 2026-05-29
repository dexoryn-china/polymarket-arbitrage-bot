"""Simple in-process metrics collection.

Stores counters, gauges, and histograms in memory.
Can be dumped to JSON for reporting.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

_MAX_EVENTS = 10_000  # Cap event history to bound memory usage


def _percentile(sorted_data: list[float], pct: float) -> float:
    """Compute percentile from pre-sorted data using linear interpolation."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    return sorted_data[int(f)] * (c - k) + sorted_data[int(c)] * (k - f)


def _histogram_stats(values: list[float]) -> dict[str, Any]:
    """Compute histogram statistics including percentiles."""
    if not values:
        return {"count": 0, "min": 0, "max": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0}
    s = sorted(values)
    return {
        "count": len(s),
        "min": s[0],
        "max": s[-1],
        "avg": sum(s) / len(s),
        "p50": _percentile(s, 50),
        "p95": _percentile(s, 95),
        "p99": _percentile(s, 99),
    }


@dataclass
class MetricPoint:
    name: str
    value: float
    tags: dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class MetricsCollector:
    """Thread-safe in-process metrics collector."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._events: list[MetricPoint] = []

    def incr(self, name: str, value: float = 1.0, **tags: str) -> None:
        with self._lock:
            self._counters[name] += value
            self._events.append(MetricPoint(name=name, value=value, tags=tags))
            if len(self._events) > _MAX_EVENTS:
                self._events = self._events[-(_MAX_EVENTS // 2):]

    def gauge(self, name: str, value: float, **tags: str) -> None:
        with self._lock:
            self._gauges[name] = value
            self._events.append(MetricPoint(name=name, value=value, tags=tags))
            if len(self._events) > _MAX_EVENTS:
                self._events = self._events[-(_MAX_EVENTS // 2):]

    def histogram(self, name: str, value: float, **tags: str) -> None:
        with self._lock:
            self._histograms[name].append(value)
            self._events.append(MetricPoint(name=name, value=value, tags=tags))
            if len(self._events) > _MAX_EVENTS:
                self._events = self._events[-(_MAX_EVENTS // 2):]

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of all metrics with percentiles."""
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {
                    k: _histogram_stats(v)
                    for k, v in self._histograms.items()
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._events.clear()


# Global singleton
metrics = MetricsCollector()


# ── API Cost Tracking ────────────────────────────────────────────────

# Approximate per-call costs (USD) for common APIs
_DEFAULT_COSTS: dict[str, float] = {
    "gpt-4o": 0.005,                    # ~$5/1M input tokens, ~1K tokens/call
    "gpt-4o-mini": 0.0005,
    "claude-3-5-sonnet-20241022": 0.005,
    "gemini-1.5-pro": 0.003,
    "serpapi": 0.005,                    # $50/5K searches
    "bing": 0.003,
    "tavily": 0.005,
}


class CostTracker:
    """Track API costs per cycle and cumulative."""

    def __init__(self, cost_map: dict[str, float] | None = None):
        self._costs = cost_map or dict(_DEFAULT_COSTS)
        self._lock = Lock()
        self._cycle_calls: dict[str, int] = defaultdict(int)
        self._total_calls: dict[str, int] = defaultdict(int)
        self._cycle_cost: float = 0.0
        self._total_cost: float = 0.0

    def record_call(self, api_name: str, count: int = 1) -> None:
        """Record an API call and its estimated cost."""
        cost_per_call = self._costs.get(api_name, 0.001)
        with self._lock:
            self._cycle_calls[api_name] += count
            self._total_calls[api_name] += count
            self._cycle_cost += cost_per_call * count
            self._total_cost += cost_per_call * count

    def end_cycle(self) -> dict[str, Any]:
        """End the current cycle and return cost summary. Resets cycle counters."""
        with self._lock:
            summary = {
                "cycle_cost_usd": round(self._cycle_cost, 4),
                "cycle_calls": dict(self._cycle_calls),
                "total_cost_usd": round(self._total_cost, 4),
                "total_calls": dict(self._total_calls),
            }
            self._cycle_calls = defaultdict(int)
            self._cycle_cost = 0.0
            return summary

    def snapshot(self) -> dict[str, Any]:
        """Return current cost state without resetting."""
        with self._lock:
            return {
                "cycle_cost_usd": round(self._cycle_cost, 4),
                "cycle_calls": dict(self._cycle_calls),
                "total_cost_usd": round(self._total_cost, 4),
                "total_calls": dict(self._total_calls),
            }


# Global cost tracker singleton
cost_tracker = CostTracker()

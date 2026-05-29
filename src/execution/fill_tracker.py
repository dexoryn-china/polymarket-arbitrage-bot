"""Fill tracker — monitor order fills and track execution quality.

Tracks:
  - Fill rate (% of orders that fill)
  - Slippage (difference between expected and actual fill price)
  - Time to fill
  - Partial fills
  - Fill quality score

This data feeds back into execution strategy selection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class FillRecord:
    """Record of an order fill."""
    order_id: str
    market_id: str
    expected_price: float
    fill_price: float
    size_ordered: float
    size_filled: float
    is_partial: bool
    slippage: float  # fill_price - expected_price (positive = worse for buyer)
    slippage_bps: float  # slippage in basis points
    time_to_fill_secs: float
    execution_strategy: str
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    @property
    def fill_rate(self) -> float:
        if self.size_ordered > 0:
            return self.size_filled / self.size_ordered
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


@dataclass
class ExecutionQuality:
    """Aggregate execution quality metrics."""
    total_orders: int = 0
    total_fills: int = 0
    partial_fills: int = 0
    unfilled: int = 0
    avg_fill_rate: float = 0.0
    avg_slippage_bps: float = 0.0
    median_time_to_fill_secs: float = 0.0
    total_slippage_usd: float = 0.0

    # By strategy
    strategy_stats: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


class FillTracker:
    """Track and analyse order fills for execution quality."""

    def __init__(self):
        self._fills: list[FillRecord] = []
        self._pending_orders: dict[str, dict[str, Any]] = {}  # order_id -> order info

    def register_order(
        self,
        order_id: str,
        market_id: str,
        expected_price: float,
        size: float,
        strategy: str,
    ) -> None:
        """Register an order as pending."""
        self._pending_orders[order_id] = {
            "market_id": market_id,
            "expected_price": expected_price,
            "size": size,
            "strategy": strategy,
            "submitted_at": time.time(),
        }

    def record_fill(
        self,
        order_id: str,
        fill_price: float,
        size_filled: float,
    ) -> FillRecord | None:
        """Record a fill for a pending order."""
        pending = self._pending_orders.get(order_id)
        if not pending:
            log.warning("fill_tracker.unknown_order", order_id=order_id)
            return None

        expected = pending["expected_price"]
        slippage = fill_price - expected
        slippage_bps = (slippage / expected * 10000) if expected > 0 else 0
        time_to_fill = time.time() - pending["submitted_at"]

        record = FillRecord(
            order_id=order_id,
            market_id=pending["market_id"],
            expected_price=expected,
            fill_price=fill_price,
            size_ordered=pending["size"],
            size_filled=size_filled,
            is_partial=size_filled < pending["size"] * 0.99,
            slippage=slippage,
            slippage_bps=round(slippage_bps, 1),
            time_to_fill_secs=round(time_to_fill, 2),
            execution_strategy=pending["strategy"],
        )

        self._fills.append(record)

        # Remove from pending if fully filled
        if not record.is_partial:
            del self._pending_orders[order_id]

        log.info(
            "fill_tracker.recorded",
            order_id=order_id[:8],
            fill_price=fill_price,
            slippage_bps=record.slippage_bps,
            fill_rate=round(record.fill_rate, 2),
            time_secs=record.time_to_fill_secs,
        )
        return record

    def record_unfilled(self, order_id: str) -> None:
        """Record that an order expired without filling."""
        if order_id in self._pending_orders:
            pending = self._pending_orders[order_id]
            record = FillRecord(
                order_id=order_id,
                market_id=pending["market_id"],
                expected_price=pending["expected_price"],
                fill_price=0.0,
                size_ordered=pending["size"],
                size_filled=0.0,
                is_partial=False,
                slippage=0.0,
                slippage_bps=0.0,
                time_to_fill_secs=time.time() - pending["submitted_at"],
                execution_strategy=pending["strategy"],
            )
            self._fills.append(record)
            del self._pending_orders[order_id]

            log.info("fill_tracker.unfilled", order_id=order_id[:8])

    def get_quality(self, lookback_hours: float = 24.0) -> ExecutionQuality:
        """Compute execution quality metrics over recent history."""
        cutoff = time.time() - (lookback_hours * 3600)
        recent = [f for f in self._fills if f.timestamp >= cutoff]

        if not recent:
            return ExecutionQuality()

        filled = [f for f in recent if f.size_filled > 0]
        partial = [f for f in filled if f.is_partial]
        unfilled = [f for f in recent if f.size_filled == 0]

        fill_rates = [f.fill_rate for f in recent]
        slippages = [f.slippage_bps for f in filled] if filled else [0]
        fill_times = sorted([f.time_to_fill_secs for f in filled]) if filled else [0]

        # Aggregate by strategy
        strat_stats: dict[str, dict[str, float]] = {}
        for f in recent:
            s = f.execution_strategy
            if s not in strat_stats:
                strat_stats[s] = {
                    "count": 0,
                    "avg_slippage_bps": 0,
                    "avg_fill_rate": 0,
                    "fills": 0,
                }
            strat_stats[s]["count"] += 1
            if f.size_filled > 0:
                strat_stats[s]["fills"] += 1
                strat_stats[s]["avg_slippage_bps"] += f.slippage_bps
                strat_stats[s]["avg_fill_rate"] += f.fill_rate

        for s, stats in strat_stats.items():
            n = stats["fills"] or 1
            stats["avg_slippage_bps"] = round(stats["avg_slippage_bps"] / n, 1)
            stats["avg_fill_rate"] = round(stats["avg_fill_rate"] / n, 2)

        quality = ExecutionQuality(
            total_orders=len(recent),
            total_fills=len(filled),
            partial_fills=len(partial),
            unfilled=len(unfilled),
            avg_fill_rate=round(sum(fill_rates) / len(fill_rates), 3),
            avg_slippage_bps=round(sum(slippages) / len(slippages), 1),
            median_time_to_fill_secs=fill_times[len(fill_times) // 2],
            total_slippage_usd=round(
                sum(f.slippage * f.size_filled for f in filled), 2
            ),
            strategy_stats=strat_stats,
        )

        return quality

    def get_recent_fills(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent fills for dashboard."""
        return [f.to_dict() for f in self._fills[-limit:]]

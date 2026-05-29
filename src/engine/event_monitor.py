"""Event monitor — triggers re-research when market conditions change.

Monitors for:
  - Large price moves (>5% in short window)
  - Volume spikes (>3x average)
  - News events (keyword changes in market metadata)
  - Approaching resolution (urgency transitions)
  - Whale activity (from microstructure)

When triggered, the engine should re-run the research pipeline
for affected markets to update forecasts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class EventTrigger:
    """An event that warrants re-research."""
    market_id: str
    trigger_type: str  # "price_move" | "volume_spike" | "resolution_approaching" | "whale_activity"
    severity: str  # "low" | "medium" | "high"
    details: str
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


class EventMonitor:
    """Monitor market events and trigger re-research."""

    def __init__(
        self,
        price_move_threshold: float = 0.05,
        volume_spike_multiplier: float = 3.0,
        cooldown_secs: float = 900,  # 15 min between same-market triggers
    ):
        self.price_move_threshold = price_move_threshold
        self.volume_spike_multiplier = volume_spike_multiplier
        self.cooldown_secs = cooldown_secs

        # State tracking
        self._last_prices: dict[str, float] = {}
        self._avg_volumes: dict[str, float] = {}
        self._last_trigger_time: dict[str, float] = {}
        self._trigger_history: list[EventTrigger] = []

    def check_price_move(
        self,
        market_id: str,
        current_price: float,
    ) -> EventTrigger | None:
        """Check if a market has had a significant price move."""
        last = self._last_prices.get(market_id)
        self._last_prices[market_id] = current_price

        if last is None:
            return None

        move = abs(current_price - last)
        if move >= self.price_move_threshold:
            if self._is_on_cooldown(market_id):
                return None

            direction = "up" if current_price > last else "down"
            severity = "high" if move >= 0.10 else "medium"

            trigger = EventTrigger(
                market_id=market_id,
                trigger_type="price_move",
                severity=severity,
                details=(
                    f"Price moved {direction} {move:.1%}: "
                    f"{last:.3f} → {current_price:.3f}"
                ),
            )
            self._record_trigger(trigger)
            return trigger
        return None

    def check_volume_spike(
        self,
        market_id: str,
        recent_volume: float,
    ) -> EventTrigger | None:
        """Check if market volume has spiked."""
        avg = self._avg_volumes.get(market_id)

        # Update rolling average
        if avg is None:
            self._avg_volumes[market_id] = recent_volume
            return None
        else:
            # Exponential moving average
            self._avg_volumes[market_id] = avg * 0.9 + recent_volume * 0.1

        if avg > 0 and recent_volume > avg * self.volume_spike_multiplier:
            if self._is_on_cooldown(market_id):
                return None

            trigger = EventTrigger(
                market_id=market_id,
                trigger_type="volume_spike",
                severity="medium",
                details=(
                    f"Volume spike: {recent_volume:.0f} vs "
                    f"avg {avg:.0f} ({recent_volume/avg:.1f}x)"
                ),
            )
            self._record_trigger(trigger)
            return trigger
        return None

    def check_resolution_approaching(
        self,
        market_id: str,
        hours_to_resolution: float,
    ) -> EventTrigger | None:
        """Check if a market is approaching resolution and needs attention."""
        # Trigger at key thresholds: 48h, 24h, 12h, 6h
        thresholds = [48, 24, 12, 6]

        for thresh in thresholds:
            # Check if we just crossed this threshold
            key = f"{market_id}_res_{thresh}"
            if hours_to_resolution <= thresh and key not in self._last_trigger_time:
                self._last_trigger_time[key] = time.time()

                severity = "high" if thresh <= 12 else "medium"
                trigger = EventTrigger(
                    market_id=market_id,
                    trigger_type="resolution_approaching",
                    severity=severity,
                    details=f"Market resolves in {hours_to_resolution:.1f}h (crossed {thresh}h threshold)",
                )
                self._record_trigger(trigger)
                return trigger

        return None

    def check_whale_activity(
        self,
        market_id: str,
        whale_count: int,
        whale_volume_pct: float,
    ) -> EventTrigger | None:
        """Check if significant whale activity detected."""
        if whale_count >= 3 or whale_volume_pct >= 0.3:
            if self._is_on_cooldown(market_id):
                return None

            severity = "high" if whale_volume_pct >= 0.5 else "medium"
            trigger = EventTrigger(
                market_id=market_id,
                trigger_type="whale_activity",
                severity=severity,
                details=(
                    f"{whale_count} whale trades, "
                    f"{whale_volume_pct:.0%} of volume"
                ),
            )
            self._record_trigger(trigger)
            return trigger
        return None

    def get_all_triggers(
        self,
        market_ids: list[str] | None = None,
        since: float | None = None,
    ) -> list[EventTrigger]:
        """Get recent triggers, optionally filtered."""
        triggers = self._trigger_history
        if market_ids:
            triggers = [t for t in triggers if t.market_id in market_ids]
        if since:
            triggers = [t for t in triggers if t.timestamp >= since]
        return triggers

    def _is_on_cooldown(self, market_id: str) -> bool:
        """Check if market is in cooldown period."""
        last = self._last_trigger_time.get(market_id, 0)
        return (time.time() - last) < self.cooldown_secs

    def _record_trigger(self, trigger: EventTrigger) -> None:
        """Record a trigger and update cooldown."""
        self._last_trigger_time[trigger.market_id] = trigger.timestamp
        self._trigger_history.append(trigger)

        # Trim history
        if len(self._trigger_history) > 500:
            self._trigger_history = self._trigger_history[-250:]

        log.info(
            "event_monitor.trigger",
            market_id=trigger.market_id,
            type=trigger.trigger_type,
            severity=trigger.severity,
            details=trigger.details,
        )

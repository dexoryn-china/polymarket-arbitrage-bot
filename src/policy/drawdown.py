"""Drawdown management — track equity curve, heat system, auto kill-switch.

The "heat system" reduces position sizing as drawdown deepens:
  - Level 1 (e.g. 5% DD):  Kelly multiplied by 0.75
  - Level 2 (e.g. 10% DD): Kelly multiplied by 0.50
  - Level 3 (e.g. 15% DD): Kelly multiplied by 0.25
  - Kill switch (e.g. 25% DD): all trading halted

This prevents ruin and automatically de-risks during adverse periods.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.config import load_config
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class DrawdownState:
    """Current drawdown state."""
    peak_equity: float = 0.0
    current_equity: float = 0.0
    drawdown_pct: float = 0.0
    heat_level: int = 0  # 0 = cool, 1-3 = progressively heated
    kelly_multiplier: float = 1.0
    is_killed: bool = False  # kill switch engaged
    kill_switch_pct: float = 0.25
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def drawdown_usd(self) -> float:
        return max(0, self.peak_equity - self.current_equity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peak_equity": self.peak_equity,
            "current_equity": self.current_equity,
            "drawdown_pct": round(self.drawdown_pct, 4),
            "drawdown_usd": round(self.drawdown_usd, 2),
            "heat_level": self.heat_level,
            "kelly_multiplier": self.kelly_multiplier,
            "is_killed": self.is_killed,
        }


class DrawdownManager:
    """Track equity curve and manage drawdown-based risk reduction."""

    def __init__(self, initial_equity: float, config: Any | None = None):
        cfg = config or load_config()
        dd = cfg.drawdown

        self.state = DrawdownState(
            peak_equity=initial_equity,
            current_equity=initial_equity,
        )

        # Heat levels: drawdown % thresholds and corresponding Kelly multipliers
        self.heat_levels = [
            dd.warning_drawdown_pct,    # e.g. 0.10
            dd.critical_drawdown_pct,   # e.g. 0.15
            dd.max_drawdown_pct,        # e.g. 0.20
        ]
        self.heat_multipliers = [
            dd.auto_reduce_at_warning,   # e.g. 0.50
            dd.auto_reduce_at_critical,  # e.g. 0.25
            0.0,                         # full stop at max drawdown
        ]
        self.max_drawdown = dd.max_drawdown_pct  # e.g. 0.20
        self.kill_switch_pct = dd.max_drawdown_pct  # kill switch at max drawdown
        self.state.kill_switch_pct = self.kill_switch_pct

    def update(self, current_equity: float) -> DrawdownState:
        """Update equity and recalculate drawdown state."""
        self.state.current_equity = current_equity

        # Update high water mark
        if current_equity > self.state.peak_equity:
            self.state.peak_equity = current_equity

        # Calculate drawdown
        if self.state.peak_equity > 0:
            self.state.drawdown_pct = (
                (self.state.peak_equity - current_equity) / self.state.peak_equity
            )
        else:
            self.state.drawdown_pct = 0.0

        # Determine heat level
        old_heat = self.state.heat_level
        self.state.heat_level = 0
        self.state.kelly_multiplier = 1.0

        for i, threshold in enumerate(self.heat_levels):
            if self.state.drawdown_pct >= threshold:
                self.state.heat_level = i + 1
                if i < len(self.heat_multipliers):
                    self.state.kelly_multiplier = self.heat_multipliers[i]

        # Kill switch
        if self.state.drawdown_pct >= self.kill_switch_pct:
            self.state.is_killed = True
            self.state.kelly_multiplier = 0.0
            log.critical(
                "drawdown.kill_switch",
                drawdown_pct=round(self.state.drawdown_pct, 4),
                peak=self.state.peak_equity,
                current=current_equity,
            )

        # Log heat level changes
        if self.state.heat_level != old_heat:
            log.warning(
                "drawdown.heat_change",
                old_level=old_heat,
                new_level=self.state.heat_level,
                drawdown_pct=round(self.state.drawdown_pct, 4),
                kelly_mult=self.state.kelly_multiplier,
            )

        # Record history point
        self.state.history.append({
            "ts": time.time(),
            "equity": current_equity,
            "drawdown_pct": self.state.drawdown_pct,
            "heat_level": self.state.heat_level,
        })

        # Trim history to last 1000 points
        if len(self.state.history) > 1000:
            self.state.history = self.state.history[-500:]

        return self.state

    def can_trade(self) -> tuple[bool, str]:
        """Check if trading is allowed under current drawdown state."""
        if self.state.is_killed:
            return False, (
                f"Kill switch engaged at {self.state.drawdown_pct:.1%} drawdown "
                f"(threshold: {self.kill_switch_pct:.0%})"
            )
        if self.state.drawdown_pct >= self.max_drawdown:
            return False, (
                f"Max drawdown {self.state.drawdown_pct:.1%} reached "
                f"(limit: {self.max_drawdown:.0%})"
            )
        return True, "ok"

    def reset_kill_switch(self) -> None:
        """Manually reset kill switch after review."""
        if self.state.is_killed:
            log.warning(
                "drawdown.kill_switch_reset",
                drawdown_pct=round(self.state.drawdown_pct, 4),
            )
            self.state.is_killed = False
            # Recalculate heat level
            self.update(self.state.current_equity)

    def get_sizing_multiplier(self) -> float:
        """Get the Kelly multiplier to apply to position sizing."""
        return self.state.kelly_multiplier

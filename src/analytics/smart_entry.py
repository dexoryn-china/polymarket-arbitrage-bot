"""Smart entry timing — calculate optimal entry levels.

Instead of entering at the current market price, this module calculates
optimal entry levels using:
  1. Orderbook depth analysis (support/resistance levels)
  2. VWAP divergence (enter when price is below VWAP for buys)
  3. Microstructure signals (flow imbalance, whale activity)
  4. Price momentum (wait for momentum confirmation or reversal)
  5. Limit order placement at calculated levels

This can significantly improve entry prices and reduce slippage,
adding 1-3% edge on every trade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class EntryLevel:
    """A calculated price level for entry."""
    price: float
    confidence: float  # 0-1, how confident this is a good level
    reason: str
    urgency: str = "normal"  # "immediate" | "normal" | "patient"
    size_fraction: float = 1.0  # fraction of total size at this level

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": round(self.price, 4),
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "urgency": self.urgency,
            "size_fraction": round(self.size_fraction, 3),
        }


@dataclass
class SmartEntryPlan:
    """Complete entry plan with one or more price levels."""
    market_id: str
    side: str  # "BUY_YES" | "BUY_NO"
    current_price: float
    fair_value: float  # model probability

    # Calculated entry levels (best to worst)
    entry_levels: list[EntryLevel] = field(default_factory=list)

    # Overall recommendation
    recommended_price: float = 0.0
    recommended_strategy: str = "limit"  # "limit" | "market" | "twap" | "patient"
    expected_improvement_bps: float = 0.0  # basis points vs market entry
    max_wait_minutes: int = 60

    # Signals that informed the plan
    vwap_signal: str = ""
    depth_signal: str = ""
    momentum_signal: str = ""
    flow_signal: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "side": self.side,
            "current_price": round(self.current_price, 4),
            "fair_value": round(self.fair_value, 4),
            "entry_levels": [l.to_dict() for l in self.entry_levels],
            "recommended_price": round(self.recommended_price, 4),
            "recommended_strategy": self.recommended_strategy,
            "expected_improvement_bps": round(self.expected_improvement_bps, 1),
            "max_wait_minutes": self.max_wait_minutes,
            "vwap_signal": self.vwap_signal,
            "depth_signal": self.depth_signal,
            "momentum_signal": self.momentum_signal,
            "flow_signal": self.flow_signal,
        }


class SmartEntryCalculator:
    """Calculate optimal entry levels for trades.

    Uses orderbook, VWAP, and momentum data to find better
    entry prices than the current market price.
    """

    def __init__(
        self,
        max_improvement_pct: float = 0.03,
        min_edge_for_market_order: float = 0.10,
        patience_factor: float = 1.0,
    ):
        """
        Args:
            max_improvement_pct: Max price improvement to target (3% default)
            min_edge_for_market_order: If edge > this, just take market price
            patience_factor: >1 = more patient, <1 = more aggressive
        """
        self._max_improvement = max_improvement_pct
        self._min_edge_market = min_edge_for_market_order
        self._patience = patience_factor

    def calculate_entry(
        self,
        market_id: str,
        side: str,
        current_price: float,
        fair_value: float,
        edge: float,
        bid_depth: float = 0.0,
        ask_depth: float = 0.0,
        vwap: float = 0.0,
        price_momentum: float = 0.0,
        flow_imbalance: float = 0.0,
        spread: float = 0.0,
        hours_to_resolution: float = 720.0,
        regime_patience: float = 1.0,
    ) -> SmartEntryPlan:
        """Calculate smart entry plan for a trade.

        Args:
            side: "BUY_YES" or "BUY_NO"
            current_price: Current market price for the token
            fair_value: Our model's fair value (probability)
            edge: Our calculated edge
            bid_depth / ask_depth: Orderbook depth in USD
            vwap: Volume-weighted average price
            price_momentum: Recent price change rate
            flow_imbalance: Order flow imbalance (-1 to +1)
            spread: Current bid-ask spread percentage
            hours_to_resolution: Time until market resolves
            regime_patience: Multiplier from regime detector
        """
        plan = SmartEntryPlan(
            market_id=market_id,
            side=side,
            current_price=current_price,
            fair_value=fair_value,
        )

        # If edge is very large, just take the price — don't get cute
        if abs(edge) > self._min_edge_market:
            plan.recommended_price = current_price
            plan.recommended_strategy = "market"
            plan.entry_levels.append(EntryLevel(
                price=current_price,
                confidence=0.9,
                reason=f"Large edge ({edge*100:.1f}%) — take current price",
                urgency="immediate",
            ))
            log.info("smart_entry.market_order", market_id=market_id, edge=round(edge, 4))
            return plan

        # If market resolves soon, be more aggressive
        if hours_to_resolution < 24:
            plan.recommended_price = current_price
            plan.recommended_strategy = "market"
            plan.entry_levels.append(EntryLevel(
                price=current_price,
                confidence=0.8,
                reason="Near resolution — take current price",
                urgency="immediate",
            ))
            return plan

        patience = self._patience * regime_patience

        # ── Signal Analysis ──────────────────────────────────────

        # 1. VWAP signal
        vwap_score = 0.0
        if vwap > 0:
            vwap_div = (current_price - vwap) / vwap
            if side == "BUY_YES":
                if current_price < vwap:
                    vwap_score = 0.3  # below VWAP = good for buying
                    plan.vwap_signal = f"Price below VWAP ({vwap_div*100:+.1f}%) — favorable"
                else:
                    vwap_score = -0.2
                    plan.vwap_signal = f"Price above VWAP ({vwap_div*100:+.1f}%) — wait for dip"
            else:
                if current_price > vwap:
                    vwap_score = 0.3
                    plan.vwap_signal = f"Price above VWAP — favorable for NO"
                else:
                    vwap_score = -0.2
                    plan.vwap_signal = f"Price below VWAP — wait for bounce"

        # 2. Depth signal
        depth_score = 0.0
        if bid_depth > 0 and ask_depth > 0:
            depth_ratio = bid_depth / ask_depth
            if side == "BUY_YES":
                if depth_ratio > 1.5:
                    depth_score = 0.2  # More bids = price likely to hold/rise
                    plan.depth_signal = f"Strong bid support ({depth_ratio:.1f}x ratio)"
                elif depth_ratio < 0.7:
                    depth_score = -0.3  # Thin bids = might dip
                    plan.depth_signal = f"Weak bid support ({depth_ratio:.1f}x ratio) — expect dip"
            else:
                if depth_ratio < 0.7:
                    depth_score = 0.2
                    plan.depth_signal = f"Weak bid side favors NO entry"
                elif depth_ratio > 1.5:
                    depth_score = -0.2
                    plan.depth_signal = f"Strong bid side — price may rise against NO"

        # 3. Momentum signal
        momentum_score = 0.0
        if abs(price_momentum) > 0.01:
            if side == "BUY_YES":
                if price_momentum < -0.02:
                    momentum_score = -0.3  # Price falling — wait
                    plan.momentum_signal = f"Negative momentum ({price_momentum*100:+.1f}%) — wait"
                elif price_momentum > 0.02:
                    momentum_score = 0.2
                    plan.momentum_signal = f"Positive momentum ({price_momentum*100:+.1f}%) — enter now"
            else:
                if price_momentum > 0.02:
                    momentum_score = -0.3
                    plan.momentum_signal = f"Positive momentum — unfavorable for NO"
                elif price_momentum < -0.02:
                    momentum_score = 0.2
                    plan.momentum_signal = f"Negative momentum — favorable for NO"

        # 4. Flow signal
        flow_score = 0.0
        if abs(flow_imbalance) > 0.1:
            if side == "BUY_YES" and flow_imbalance > 0.2:
                flow_score = 0.1
                plan.flow_signal = f"Buy flow imbalance ({flow_imbalance:+.2f}) — smart money buying"
            elif side == "BUY_YES" and flow_imbalance < -0.2:
                flow_score = -0.2
                plan.flow_signal = f"Sell flow imbalance ({flow_imbalance:+.2f}) — wait"

        # ── Compute Entry Levels ─────────────────────────────────

        # Aggregate signal: negative = be patient, positive = enter now
        signal_sum = vwap_score + depth_score + momentum_score + flow_score

        if signal_sum > 0.3:
            # Signals say enter now — use current price or slightly better
            improvement = spread * 0.3  # try to capture some of the spread
            entry_price = _adjust_price(current_price, side, -improvement)
            plan.recommended_strategy = "limit"
            plan.max_wait_minutes = int(15 * patience)
            plan.entry_levels.append(EntryLevel(
                price=entry_price,
                confidence=0.8,
                reason="Favorable signals — aggressive limit order",
                urgency="normal",
            ))
            plan.entry_levels.append(EntryLevel(
                price=current_price,
                confidence=0.9,
                reason="Fallback: take current price",
                urgency="immediate",
                size_fraction=0.5,
            ))
        elif signal_sum < -0.2:
            # Signals say wait — target a better price
            target_improvement = min(self._max_improvement, spread + 0.005)
            patient_price = _adjust_price(current_price, side, -target_improvement)
            mid_price = _adjust_price(current_price, side, -target_improvement * 0.5)

            plan.recommended_strategy = "patient"
            plan.max_wait_minutes = int(60 * patience)

            # Tiered entry: smaller order at better price, larger at mid
            plan.entry_levels.append(EntryLevel(
                price=patient_price,
                confidence=0.5,
                reason="Patient level — best price target",
                urgency="patient",
                size_fraction=0.3,
            ))
            plan.entry_levels.append(EntryLevel(
                price=mid_price,
                confidence=0.7,
                reason="Mid level — moderate improvement",
                urgency="normal",
                size_fraction=0.4,
            ))
            plan.entry_levels.append(EntryLevel(
                price=current_price,
                confidence=0.9,
                reason="Fallback: take current price if levels don't fill",
                urgency="immediate",
                size_fraction=0.3,
            ))
        else:
            # Neutral — standard limit order at slight improvement
            improvement = spread * 0.2
            entry_price = _adjust_price(current_price, side, -improvement)
            plan.recommended_strategy = "limit"
            plan.max_wait_minutes = int(30 * patience)
            plan.entry_levels.append(EntryLevel(
                price=entry_price,
                confidence=0.75,
                reason="Neutral signals — standard limit order",
                urgency="normal",
            ))

        # Set recommended price as the highest-confidence level
        if plan.entry_levels:
            best = max(plan.entry_levels, key=lambda l: l.confidence * l.size_fraction)
            plan.recommended_price = best.price

            # Calculate expected improvement
            diff = abs(current_price - plan.recommended_price)
            plan.expected_improvement_bps = diff * 10000  # convert to basis points

        log.info(
            "smart_entry.plan",
            market_id=market_id,
            strategy=plan.recommended_strategy,
            improvement_bps=round(plan.expected_improvement_bps, 1),
            levels=len(plan.entry_levels),
            signal_sum=round(signal_sum, 3),
        )

        return plan


def _adjust_price(price: float, side: str, adjustment: float) -> float:
    """Adjust price based on side. For BUY_YES, lower is better.
    For BUY_NO, higher YES price is better (we want YES price to go up)."""
    if side == "BUY_YES":
        return max(0.01, min(0.99, price + adjustment))
    else:
        # For NO positions, we benefit from lower YES prices
        return max(0.01, min(0.99, price - adjustment))

"""Market microstructure analysis — order flow, whale detection, VWAP.

Extracts alpha signals from:
  - Order flow imbalance across multiple time windows
  - VWAP divergence from current price
  - Whale order detection (large individual trades)
  - Trade arrival rate acceleration
  - Book depth ratio changes
  - Smart money vs retail flow estimation
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from src.config import MicrostructureConfig
from src.connectors.polymarket_clob import OrderBook, TradeRecord
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class FlowImbalance:
    """Order flow imbalance for a time window."""
    window_minutes: int
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    net_flow: float = 0.0
    imbalance_ratio: float = 0.0  # -1 (all sells) to +1 (all buys)
    num_trades: int = 0


@dataclass
class WhaleAlert:
    """Detection of a large order."""
    size_usd: float
    side: str
    price: float
    timestamp: float
    is_whale: bool = True


@dataclass
class MicrostructureSignals:
    """Complete microstructure signal package for a token."""
    token_id: str

    # VWAP signals
    vwap: float = 0.0
    vwap_divergence: float = 0.0  # current_price - vwap (positive = above vwap)
    vwap_divergence_pct: float = 0.0

    # Flow imbalance across windows
    flow_imbalances: list[FlowImbalance] = field(default_factory=list)

    # Whale activity
    whale_alerts: list[WhaleAlert] = field(default_factory=list)
    whale_buy_volume: float = 0.0
    whale_sell_volume: float = 0.0
    whale_net_flow: float = 0.0

    # Trade acceleration
    trade_rate_current: float = 0.0  # trades per minute (recent window)
    trade_rate_baseline: float = 0.0  # trades per minute (longer window)
    trade_acceleration: float = 0.0  # current / baseline

    # Book pressure
    bid_depth_total: float = 0.0
    ask_depth_total: float = 0.0
    depth_ratio: float = 0.0  # bid_depth / ask_depth (>1 = more buy pressure)
    depth_imbalance: float = 0.0  # (bid - ask) / (bid + ask)

    # Smart money estimate
    large_trade_direction: str = "neutral"  # "bullish" | "bearish" | "neutral"
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "vwap": round(self.vwap, 4),
            "vwap_divergence_pct": round(self.vwap_divergence_pct, 4),
            "flow_imbalances": [
                {
                    "window_min": fi.window_minutes,
                    "imbalance": round(fi.imbalance_ratio, 3),
                    "net_flow": round(fi.net_flow, 2),
                }
                for fi in self.flow_imbalances
            ],
            "whale_count": len(self.whale_alerts),
            "whale_net_flow": round(self.whale_net_flow, 2),
            "trade_acceleration": round(self.trade_acceleration, 2),
            "depth_ratio": round(self.depth_ratio, 3),
            "large_trade_direction": self.large_trade_direction,
        }


def analyze_microstructure(
    token_id: str,
    orderbook: OrderBook,
    trades: list[TradeRecord],
    config: MicrostructureConfig,
) -> MicrostructureSignals:
    """Compute microstructure signals from orderbook and trade data."""
    signals = MicrostructureSignals(token_id=token_id)

    # ── VWAP Calculation ─────────────────────────────────────────
    lookback = config.vwap_lookback_trades
    recent_trades = trades[:lookback] if trades else []

    if recent_trades:
        total_volume = sum(t.size for t in recent_trades if t.size > 0)
        if total_volume > 0:
            weighted_sum = sum(t.price * t.size for t in recent_trades if t.size > 0)
            signals.vwap = weighted_sum / total_volume
            current_price = orderbook.mid if orderbook.mid > 0 else recent_trades[0].price
            signals.vwap_divergence = current_price - signals.vwap
            if signals.vwap > 0:
                signals.vwap_divergence_pct = signals.vwap_divergence / signals.vwap

    # ── Flow Imbalance ───────────────────────────────────────────
    now = time.time()
    for window_min in config.flow_imbalance_windows:
        window_secs = window_min * 60
        cutoff = now - window_secs
        window_trades = [t for t in trades if t.timestamp >= cutoff]

        buy_vol = sum(t.size * t.price for t in window_trades if t.side.lower() == "buy")
        sell_vol = sum(t.size * t.price for t in window_trades if t.side.lower() == "sell")
        total_vol = buy_vol + sell_vol

        fi = FlowImbalance(
            window_minutes=window_min,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            net_flow=buy_vol - sell_vol,
            imbalance_ratio=(buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0,
            num_trades=len(window_trades),
        )
        signals.flow_imbalances.append(fi)

    # ── Whale Detection ──────────────────────────────────────────
    whale_threshold = config.whale_size_threshold_usd
    for t in trades:
        notional = t.size * t.price
        if notional >= whale_threshold:
            alert = WhaleAlert(
                size_usd=notional,
                side=t.side,
                price=t.price,
                timestamp=t.timestamp,
            )
            signals.whale_alerts.append(alert)
            if t.side.lower() == "buy":
                signals.whale_buy_volume += notional
            else:
                signals.whale_sell_volume += notional

    signals.whale_net_flow = signals.whale_buy_volume - signals.whale_sell_volume

    # ── Trade Acceleration ───────────────────────────────────────
    accel_window = config.trade_acceleration_window_mins * 60
    accel_cutoff = now - accel_window
    baseline_cutoff = now - (accel_window * 4)

    recent_count = sum(1 for t in trades if t.timestamp >= accel_cutoff)
    baseline_count = sum(1 for t in trades if baseline_cutoff <= t.timestamp < accel_cutoff)

    if config.trade_acceleration_window_mins > 0:
        signals.trade_rate_current = recent_count / config.trade_acceleration_window_mins
        baseline_mins = config.trade_acceleration_window_mins * 3
        signals.trade_rate_baseline = baseline_count / baseline_mins if baseline_mins > 0 else 0
        if signals.trade_rate_baseline > 0:
            signals.trade_acceleration = signals.trade_rate_current / signals.trade_rate_baseline
        else:
            signals.trade_acceleration = 1.0

    # ── Book Depth Analysis ──────────────────────────────────────
    signals.bid_depth_total = orderbook.bid_depth(10)
    signals.ask_depth_total = orderbook.ask_depth(10)
    total_depth = signals.bid_depth_total + signals.ask_depth_total

    if signals.ask_depth_total > 0:
        signals.depth_ratio = signals.bid_depth_total / signals.ask_depth_total
    if total_depth > 0:
        signals.depth_imbalance = (
            (signals.bid_depth_total - signals.ask_depth_total) / total_depth
        )

    # ── Smart Money Direction ────────────────────────────────────
    # Heuristic: whale flow + book depth + acceleration → direction
    direction_score = 0.0
    if signals.whale_net_flow > whale_threshold * 0.5:
        direction_score += 1.0
    elif signals.whale_net_flow < -whale_threshold * 0.5:
        direction_score -= 1.0

    if signals.depth_imbalance > 0.2:
        direction_score += 0.5
    elif signals.depth_imbalance < -0.2:
        direction_score -= 0.5

    # Flow imbalance from shortest window
    if signals.flow_imbalances:
        shortest = signals.flow_imbalances[0]
        direction_score += shortest.imbalance_ratio * 0.5

    if direction_score > 0.5:
        signals.large_trade_direction = "bullish"
        signals.confidence = min(1.0, direction_score / 2.0)
    elif direction_score < -0.5:
        signals.large_trade_direction = "bearish"
        signals.confidence = min(1.0, abs(direction_score) / 2.0)
    else:
        signals.large_trade_direction = "neutral"
        signals.confidence = 0.0

    log.info(
        "microstructure.analyzed",
        token_id=token_id,
        vwap_div=round(signals.vwap_divergence_pct, 4),
        whales=len(signals.whale_alerts),
        acceleration=round(signals.trade_acceleration, 2),
        direction=signals.large_trade_direction,
    )
    return signals

"""Position sizer — determines optimal trade size.

Uses fractional Kelly criterion with guardrails:
  - Full Kelly: f* = (p * b - q) / b
  - Fractional Kelly: f* × kelly_fraction (default 0.25)
  - Drawdown heat multiplier: reduces sizing during drawdowns
  - Timeline multiplier: adjusts for resolution timing
  - Volatility adjustment: reduces sizing in volatile markets
  - Portfolio risk gate: checks category/event exposure before sizing
  - Capped by max_stake_per_market and max_bankroll_fraction
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config import RiskConfig
from src.policy.edge_calc import EdgeResult
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class PositionSize:
    """Computed position size."""
    stake_usd: float
    kelly_fraction_used: float
    full_kelly_stake: float
    capped_by: str  # "kelly" | "max_stake" | "max_bankroll" | "drawdown" | "portfolio"
    direction: str  # "BUY_YES" | "BUY_NO"
    token_quantity: float  # approximate tokens at implied price

    # Adjustment breakdown
    base_kelly: float = 0.0
    confidence_mult: float = 1.0
    drawdown_mult: float = 1.0
    timeline_mult: float = 1.0
    volatility_mult: float = 1.0
    portfolio_gate: str = "ok"  # "ok" or reason for rejection

    def to_dict(self) -> dict:
        return self.__dict__


def calculate_position_size(
    edge: EdgeResult,
    risk_config: RiskConfig,
    confidence_level: str = "LOW",
    drawdown_multiplier: float = 1.0,
    timeline_multiplier: float = 1.0,
    price_volatility: float = 0.0,
    portfolio_gate: tuple[bool, str] = (True, "ok"),
    regime_multiplier: float = 1.0,
    category_multiplier: float = 1.0,
    liquidity_usd: float = 0.0,
    max_liquidity_pct: float = 0.05,
) -> PositionSize:
    """Calculate position size using fractional Kelly with drawdown + timeline adjustments.

    Kelly formula for binary outcome:
      f* = (p * b - q) / b
    where:
      p = model probability of winning
      b = odds (payout / cost - 1)
      q = 1 - p

    Additional multipliers:
      - confidence_level: LOW=0.5, MEDIUM=0.75, HIGH=1.0
      - drawdown_multiplier: from DrawdownManager (0-1)
      - timeline_multiplier: from TimelineAssessment (0.5-1.3)
      - volatility adjustment: reduces sizing for volatile markets
      - liquidity_usd: available liquidity; stake capped to max_liquidity_pct × liquidity
    """
    # Portfolio gate check
    can_trade, gate_reason = portfolio_gate
    if not can_trade:
        return PositionSize(
            stake_usd=0.0,
            kelly_fraction_used=0.0,
            full_kelly_stake=0.0,
            capped_by="portfolio",
            direction=edge.direction,
            token_quantity=0.0,
            portfolio_gate=gate_reason,
        )

    if edge.direction == "BUY_YES":
        p = edge.model_probability
        cost = edge.implied_probability
    else:
        p = 1 - edge.model_probability
        cost = 1 - edge.implied_probability

    cost = max(cost, 0.01)
    b = (1.0 / cost) - 1.0  # odds
    q = 1 - p

    # Full Kelly
    if b > 0:
        full_kelly_frac = (p * b - q) / b
    else:
        full_kelly_frac = 0.0

    full_kelly_frac = max(0.0, full_kelly_frac)
    base_kelly = full_kelly_frac

    # Confidence multiplier
    kelly_mult = risk_config.kelly_fraction
    if confidence_level == "LOW":
        conf_mult = 0.5
    elif confidence_level == "MEDIUM":
        conf_mult = 0.75
    else:
        conf_mult = 1.0
    kelly_mult *= conf_mult

    # Volatility adjustment: reduce sizing for volatile markets
    vol_mult = 1.0
    if price_volatility > risk_config.volatility_high_threshold:
        vol_mult = max(
            risk_config.volatility_high_min_mult,
            1.0 - (price_volatility - risk_config.volatility_high_threshold) * 2,
        )
    elif price_volatility > risk_config.volatility_med_threshold:
        vol_mult = max(
            risk_config.volatility_med_min_mult,
            1.0 - (price_volatility - risk_config.volatility_med_threshold) * 3,
        )

    # Apply all multipliers
    combined_mult = kelly_mult * drawdown_multiplier * timeline_multiplier * vol_mult * regime_multiplier * category_multiplier
    adj_kelly = full_kelly_frac * combined_mult
    full_kelly_stake = adj_kelly * risk_config.bankroll

    # Apply caps
    max_stake = risk_config.max_stake_per_market
    max_bankroll = risk_config.max_bankroll_fraction * risk_config.bankroll

    # Liquidity-adjusted cap: never take more than X% of available liquidity
    max_liquidity = float("inf")
    if liquidity_usd > 0:
        max_liquidity = liquidity_usd * max_liquidity_pct

    stake = min(full_kelly_stake, max_stake, max_bankroll, max_liquidity)
    stake = max(0.0, stake)

    # Enforce minimum stake floor to avoid dust trades
    if 0 < stake < risk_config.min_stake_usd:
        stake = 0.0

    # Determine what capped it
    if drawdown_multiplier <= 0:
        capped_by = "drawdown"
    elif stake == 0.0 and full_kelly_stake > 0:
        capped_by = "min_stake"
    elif stake >= full_kelly_stake - 0.01:
        capped_by = "kelly"
    elif stake >= max_stake - 0.01:
        capped_by = "max_stake"
    elif liquidity_usd > 0 and max_liquidity < float("inf") and stake >= max_liquidity - 0.01:
        capped_by = "liquidity"
    else:
        capped_by = "max_bankroll"

    # Approximate token quantity
    token_qty = stake / cost if cost > 0 else 0.0

    result = PositionSize(
        stake_usd=round(stake, 2),
        kelly_fraction_used=round(adj_kelly, 4),
        full_kelly_stake=round(full_kelly_stake, 2),
        capped_by=capped_by,
        direction=edge.direction,
        token_quantity=round(token_qty, 2),
        base_kelly=round(base_kelly, 4),
        confidence_mult=round(conf_mult, 2),
        drawdown_mult=round(drawdown_multiplier, 2),
        timeline_mult=round(timeline_multiplier, 2),
        volatility_mult=round(vol_mult, 2),
        portfolio_gate=gate_reason,
    )

    log.info(
        "position_sizer.sized",
        stake=result.stake_usd,
        kelly_frac=result.kelly_fraction_used,
        full_kelly=result.full_kelly_stake,
        capped_by=result.capped_by,
        direction=result.direction,
        dd_mult=round(drawdown_multiplier, 2),
        tl_mult=round(timeline_multiplier, 2),
        vol_mult=round(vol_mult, 2),
        regime_mult=round(regime_multiplier, 2),
        category_mult=round(category_multiplier, 2),
    )
    return result

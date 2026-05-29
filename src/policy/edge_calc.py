"""Edge calculator — computes trading edge from forecast vs market price.

Edge = model_probability - implied_probability

Features:
  - Directional edge (buy YES or buy NO)
  - Transaction cost deduction (fees + gas)
  - Multi-outcome market support
  - Expected value per dollar (net of costs)
  - Confidence-weighted edge
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class EdgeResult:
    """Result of edge calculation."""
    implied_probability: float
    model_probability: float
    raw_edge: float              # model_prob - implied_prob
    edge_pct: float              # raw_edge / implied_prob
    direction: str               # "BUY_YES" | "BUY_NO"
    expected_value_per_dollar: float
    is_positive: bool
    # Cost-adjusted fields
    transaction_cost_pct: float = 0.0
    net_edge: float = 0.0        # raw_edge - transaction_cost
    net_ev_per_dollar: float = 0.0
    break_even_probability: float = 0.0

    @property
    def abs_edge(self) -> float:
        return abs(self.raw_edge)

    @property
    def abs_net_edge(self) -> float:
        return abs(self.net_edge)


@dataclass
class MultiOutcomeEdge:
    """Edge calculation for multi-outcome markets."""
    market_id: str
    outcomes: List[str] = field(default_factory=list)
    implied_probs: List[float] = field(default_factory=list)
    model_probs: List[float] = field(default_factory=list)
    edges: List[float] = field(default_factory=list)
    best_outcome_index: int = -1
    best_edge: float = 0.0
    best_direction: str = ""
    overround: float = 0.0  # sum of implied probs - 1.0 (vig)


def calculate_edge(
    implied_prob: float,
    model_prob: float,
    transaction_fee_pct: float = 0.0,
    gas_cost_usd: float = 0.0,
    stake_usd: float = 100.0,
    exit_fee_pct: float = 0.0,
    hold_to_resolution: bool = True,
    holding_hours: float = 0.0,
    annual_opportunity_cost: float = 0.05,
) -> EdgeResult:
    """Calculate trading edge with transaction cost awareness.

    If model_prob > implied_prob → edge on YES (buy YES token).
    If model_prob < implied_prob → edge on NO (buy NO token).

    Transaction costs are deducted from the edge to get net_edge.
    When hold_to_resolution is False, exit_fee_pct is included.
    A time-value-of-money discount is applied for capital lockup.
    """
    raw_edge = model_prob - implied_prob

    # Cost model: entry fee always; exit fee only when not holding to resolution
    total_cost_pct = transaction_fee_pct  # entry fee
    if not hold_to_resolution and exit_fee_pct > 0:
        total_cost_pct += exit_fee_pct  # exit/sell fee
    if stake_usd > 0:
        total_cost_pct += gas_cost_usd / stake_usd  # gas as % of stake

    # Time-value-of-money discount for capital lockup
    time_discount = 0.0
    if holding_hours > 0 and annual_opportunity_cost > 0:
        years_locked = holding_hours / (365.25 * 24)
        time_discount = annual_opportunity_cost * years_locked

    if raw_edge >= 0:
        direction = "BUY_YES"
        cost = implied_prob if implied_prob > 0 else 0.001
        ev = (model_prob * 1.0 - cost) / cost
        # Break-even: what model_prob needs to be to overcome costs
        break_even = cost * (1 + total_cost_pct)
    else:
        direction = "BUY_NO"
        cost = (1 - implied_prob) if (1 - implied_prob) > 0 else 0.001
        no_model = 1 - model_prob
        ev = (no_model * 1.0 - cost) / cost
        break_even = 1.0 - cost * (1 + total_cost_pct)

    # Net edge after costs and time discount
    net_edge = abs(raw_edge) - total_cost_pct - time_discount
    net_ev = ev - total_cost_pct - time_discount

    edge_pct = raw_edge / implied_prob if implied_prob > 0 else 0.0

    result = EdgeResult(
        implied_probability=implied_prob,
        model_probability=model_prob,
        raw_edge=raw_edge,
        edge_pct=edge_pct,
        direction=direction,
        expected_value_per_dollar=ev,
        is_positive=net_edge > 0,
        transaction_cost_pct=total_cost_pct,
        net_edge=net_edge,  # Always positive when edge is profitable
        net_ev_per_dollar=net_ev,
        break_even_probability=break_even,
    )

    log.info(
        "edge_calc.result",
        implied=round(implied_prob, 3),
        model=round(model_prob, 3),
        raw_edge=round(raw_edge, 3),
        net_edge=round(net_edge, 3),
        cost_pct=round(total_cost_pct, 4),
        time_discount=round(time_discount, 4),
        direction=direction,
        ev_per_dollar=round(ev, 3),
    )
    return result


def calculate_multi_outcome_edge(
    market_id: str,
    outcomes: list[str],
    implied_probs: list[float],
    model_probs: list[float],
    transaction_fee_pct: float = 0.0,
    exit_fee_pct: float = 0.0,
    hold_to_resolution: bool = True,
) -> MultiOutcomeEdge:
    """Calculate edge across all outcomes in a multi-outcome market.

    Identifies the best outcome to bet on and detects overround (vig).
    Cost model: entry fee always, exit fee only when not holding to resolution.
    """
    if len(outcomes) != len(implied_probs) or len(outcomes) != len(model_probs):
        raise ValueError("outcomes, implied_probs, and model_probs must have same length")

    overround = sum(implied_probs) - 1.0

    # Adjust implied probs for overround to get true implied
    adj_implied = implied_probs
    if overround > 0.01:
        total = sum(implied_probs)
        adj_implied = [p / total for p in implied_probs]

    edges = [m - i for m, i in zip(model_probs, adj_implied)]

    # Find best edge (considering costs)
    round_trip_cost = transaction_fee_pct
    if not hold_to_resolution and exit_fee_pct > 0:
        round_trip_cost += exit_fee_pct
    best_idx = -1
    best_net_edge = -float("inf")
    for i, edge in enumerate(edges):
        net = abs(edge) - round_trip_cost
        if net > best_net_edge:
            best_net_edge = net
            best_idx = i

    best_direction = ""
    if best_idx >= 0:
        if edges[best_idx] > 0:
            best_direction = f"BUY_{outcomes[best_idx].upper()}"
        else:
            best_direction = f"SELL_{outcomes[best_idx].upper()}"

    result = MultiOutcomeEdge(
        market_id=market_id,
        outcomes=outcomes,
        implied_probs=implied_probs,
        model_probs=model_probs,
        edges=edges,
        best_outcome_index=best_idx,
        best_edge=edges[best_idx] if best_idx >= 0 else 0.0,
        best_direction=best_direction,
        overround=overround,
    )

    log.info(
        "edge_calc.multi_outcome",
        market_id=market_id,
        num_outcomes=len(outcomes),
        best_outcome=outcomes[best_idx] if best_idx >= 0 else "none",
        best_edge=round(result.best_edge, 3),
        overround=round(overround, 3),
    )
    return result

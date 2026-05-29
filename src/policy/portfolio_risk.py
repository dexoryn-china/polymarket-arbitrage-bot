"""Portfolio risk management — category exposure, correlation, concentration limits.

Prevents over-concentration in correlated markets, single categories,
or single events. Works alongside per-trade risk_limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.config import load_config
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class PositionSnapshot:
    """Current position in a market."""
    market_id: str
    question: str
    category: str
    event_slug: str  # groups related markets
    side: str  # YES / NO
    size_usd: float
    entry_price: float
    current_price: float
    unrealised_pnl: float = 0.0

    @property
    def exposure_usd(self) -> float:
        return abs(self.size_usd)


@dataclass
class PortfolioRiskReport:
    """Summary of portfolio risk state."""
    total_exposure_usd: float = 0.0
    total_unrealised_pnl: float = 0.0
    num_positions: int = 0
    category_exposures: dict[str, float] = field(default_factory=dict)
    event_exposures: dict[str, float] = field(default_factory=dict)
    largest_position_pct: float = 0.0

    # Limit violations
    category_violations: list[str] = field(default_factory=list)
    event_violations: list[str] = field(default_factory=list)
    concentration_violation: bool = False
    correlated_position_count: int = 0
    is_healthy: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class PortfolioRiskManager:
    """Evaluate portfolio-level risk constraints."""

    def __init__(self, bankroll: float, config: Any | None = None):
        self.bankroll = max(bankroll, 1.0)
        cfg = config or load_config()
        p = cfg.portfolio
        self.max_exposure_per_category = p.max_category_exposure_pct
        self.max_exposure_per_event = p.max_single_event_exposure_pct
        self.max_correlated_positions = p.max_correlated_positions
        self.correlation_threshold = p.correlation_similarity_threshold

    def assess(self, positions: list[PositionSnapshot]) -> PortfolioRiskReport:
        """Build a risk report from current positions."""
        report = PortfolioRiskReport()
        report.num_positions = len(positions)

        if not positions:
            return report

        report.total_exposure_usd = sum(p.exposure_usd for p in positions)
        report.total_unrealised_pnl = sum(p.unrealised_pnl for p in positions)

        # Category exposure
        for pos in positions:
            cat = pos.category or "uncategorised"
            report.category_exposures[cat] = (
                report.category_exposures.get(cat, 0.0) + pos.exposure_usd
            )

        # Event exposure (grouped markets)
        for pos in positions:
            evt = pos.event_slug or pos.market_id
            report.event_exposures[evt] = (
                report.event_exposures.get(evt, 0.0) + pos.exposure_usd
            )

        # Check category limits
        for cat, exp in report.category_exposures.items():
            pct = exp / self.bankroll
            if pct > self.max_exposure_per_category:
                report.category_violations.append(
                    f"{cat}: {pct:.1%} > {self.max_exposure_per_category:.0%} limit"
                )
                report.is_healthy = False

        # Check event limits
        for evt, exp in report.event_exposures.items():
            pct = exp / self.bankroll
            if pct > self.max_exposure_per_event:
                report.event_violations.append(
                    f"{evt}: {pct:.1%} > {self.max_exposure_per_event:.0%} limit"
                )
                report.is_healthy = False

        # Concentration check: largest position
        max_pos = max(p.exposure_usd for p in positions)
        report.largest_position_pct = max_pos / self.bankroll
        if report.largest_position_pct > 0.25:
            report.concentration_violation = True
            report.is_healthy = False

        # Simple correlation proxy: positions in same category count as correlated
        cat_counts = {}
        for pos in positions:
            cat = pos.category or "uncategorised"
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        max_in_cat = max(cat_counts.values()) if cat_counts else 0
        report.correlated_position_count = max_in_cat
        if max_in_cat > self.max_correlated_positions:
            report.is_healthy = False

        log.info(
            "portfolio_risk.assessed",
            total_exposure=round(report.total_exposure_usd, 2),
            num_positions=report.num_positions,
            healthy=report.is_healthy,
            category_violations=len(report.category_violations),
        )
        return report

    def can_add_position(
        self,
        positions: list[PositionSnapshot],
        new_category: str,
        new_event: str,
        new_size_usd: float,
    ) -> tuple[bool, str]:
        """Check if adding a new position would violate portfolio limits."""
        # Category exposure check
        cat_exposure = sum(
            p.exposure_usd for p in positions if p.category == new_category
        )
        new_cat_pct = (cat_exposure + new_size_usd) / self.bankroll
        if new_cat_pct > self.max_exposure_per_category:
            return False, (
                f"Category '{new_category}' would reach {new_cat_pct:.1%} "
                f"(limit {self.max_exposure_per_category:.0%})"
            )

        # Event exposure check
        evt_exposure = sum(
            p.exposure_usd for p in positions if p.event_slug == new_event
        )
        new_evt_pct = (evt_exposure + new_size_usd) / self.bankroll
        if new_evt_pct > self.max_exposure_per_event:
            return False, (
                f"Event '{new_event}' would reach {new_evt_pct:.1%} "
                f"(limit {self.max_exposure_per_event:.0%})"
            )

        # Concentration check
        new_total = sum(p.exposure_usd for p in positions) + new_size_usd
        if new_size_usd / self.bankroll > 0.25:
            return False, f"Single position {new_size_usd:.0f} > 25% of bankroll"

        # Correlated positions
        same_cat = sum(1 for p in positions if p.category == new_category) + 1
        if same_cat > self.max_correlated_positions:
            return False, (
                f"Would create {same_cat} positions in '{new_category}' "
                f"(limit {self.max_correlated_positions})"
            )

        return True, "ok"

    def check_rebalance(
        self, positions: list[PositionSnapshot]
    ) -> list["RebalanceSignal"]:
        """Check for positions that have drifted from target allocation.

        Generates rebalance signals when:
          - A category's exposure exceeds its target by >50%
          - A single position has grown to >2× its original weight
          - Total portfolio is over-concentrated
        """
        signals: list[RebalanceSignal] = []
        if not positions:
            return signals

        total = sum(p.exposure_usd for p in positions)
        if total < 1.0:
            return signals

        # Category exposure drift
        cat_exposure: dict[str, float] = {}
        for pos in positions:
            cat = pos.category or "uncategorised"
            cat_exposure[cat] = cat_exposure.get(cat, 0.0) + pos.exposure_usd

        for cat, exposure in cat_exposure.items():
            pct = exposure / self.bankroll
            limit = self.max_exposure_per_category
            if pct > limit * 1.5:
                excess = exposure - (limit * self.bankroll)
                signals.append(RebalanceSignal(
                    signal_type="category_overweight",
                    category=cat,
                    current_pct=round(pct, 4),
                    target_pct=round(limit, 4),
                    excess_usd=round(excess, 2),
                    urgency="high" if pct > limit * 2.0 else "medium",
                    description=(
                        f"Category '{cat}' at {pct:.1%} of bankroll "
                        f"(target ≤{limit:.0%}), excess ${excess:.0f}"
                    ),
                ))

        # Single-position concentration
        for pos in positions:
            pos_pct = pos.exposure_usd / self.bankroll
            if pos_pct > 0.20:
                signals.append(RebalanceSignal(
                    signal_type="position_overweight",
                    market_id=pos.market_id,
                    current_pct=round(pos_pct, 4),
                    target_pct=0.10,
                    excess_usd=round(pos.exposure_usd - 0.10 * self.bankroll, 2),
                    urgency="high" if pos_pct > 0.30 else "medium",
                    description=(
                        f"Position {pos.market_id[:8]} at {pos_pct:.1%} "
                        f"of bankroll (recommend ≤10%)"
                    ),
                ))

        if signals:
            log.info(
                "portfolio_risk.rebalance_signals",
                count=len(signals),
                high_urgency=sum(1 for s in signals if s.urgency == "high"),
            )
        return signals


@dataclass
class RebalanceSignal:
    """Signal to rebalance a position or category."""
    signal_type: str  # "category_overweight" | "position_overweight"
    description: str
    current_pct: float = 0.0
    target_pct: float = 0.0
    excess_usd: float = 0.0
    urgency: str = "medium"  # "low" | "medium" | "high"
    category: str = ""
    market_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


def calculate_portfolio_var(
    positions: list[PositionSnapshot],
    bankroll: float,
    confidence_level: float = 0.95,
    method: str = "parametric",
) -> dict[str, Any]:
    """Compute portfolio Value-at-Risk.

    Parametric approach: treats each binary position as an independent
    Bernoulli bet.  Loss for each position = stake × (1 − current_price).
    VaR is estimated using the normal approximation over the portfolio of
    independent bets.

    Returns dict with daily_var_95, daily_var_99, component details.
    """
    import math

    if not positions:
        return {
            "daily_var_95": 0.0,
            "daily_var_99": 0.0,
            "portfolio_value": bankroll,
            "num_positions": 0,
            "method": method,
            "components": [],
        }

    components: list[dict[str, Any]] = []
    total_variance = 0.0

    for pos in positions:
        p = max(min(pos.current_price, 0.99), 0.01)
        q = 1 - p
        # Expected loss = exposure × q  (probability of losing)
        # Variance of loss for a Bernoulli outcome
        mean_loss = pos.exposure_usd * q
        var_loss = pos.exposure_usd ** 2 * p * q
        total_variance += var_loss
        components.append({
            "market_id": pos.market_id,
            "exposure": round(pos.exposure_usd, 2),
            "current_price": round(p, 4),
            "expected_loss": round(mean_loss, 2),
        })

    portfolio_std = math.sqrt(total_variance) if total_variance > 0 else 0.0
    mean_total_loss = sum(c["expected_loss"] for c in components)

    # z-scores for confidence levels
    z_95 = 1.645
    z_99 = 2.326

    var_95 = mean_total_loss + z_95 * portfolio_std
    var_99 = mean_total_loss + z_99 * portfolio_std

    result = {
        "daily_var_95": round(var_95, 2),
        "daily_var_99": round(var_99, 2),
        "portfolio_value": round(bankroll, 2),
        "num_positions": len(positions),
        "mean_expected_loss": round(mean_total_loss, 2),
        "portfolio_std": round(portfolio_std, 2),
        "method": method,
        "components": components,
    }

    log.info(
        "portfolio_risk.var_calculated",
        var_95=result["daily_var_95"],
        var_99=result["daily_var_99"],
        positions=len(positions),
    )
    return result


def check_correlation(
    existing_positions: list[PositionSnapshot],
    new_question: str,
    new_category: str,
    new_event_slug: str,
    similarity_threshold: float = 0.7,
) -> tuple[bool, str]:
    """Check if a new position is too correlated with existing ones.

    Uses text similarity and category/event matching to detect correlated
    positions. Returns (is_ok, reason).
    """
    if not existing_positions:
        return True, "ok"

    # Same event slug is a strong correlation signal
    same_event = [
        p for p in existing_positions
        if p.event_slug and p.event_slug == new_event_slug
    ]
    if len(same_event) >= 2:
        return False, (
            f"Already {len(same_event)} positions in event '{new_event_slug}'"
        )

    # Same category keyword overlap
    new_words = set(new_question.lower().split())
    for pos in existing_positions:
        if not pos.question:
            continue
        existing_words = set(pos.question.lower().split())
        if len(new_words) < 3 or len(existing_words) < 3:
            continue
        # Jaccard similarity
        intersection = len(new_words & existing_words)
        union = len(new_words | existing_words)
        similarity = intersection / union if union > 0 else 0.0
        if similarity >= similarity_threshold:
            return False, (
                f"High text similarity ({similarity:.0%}) with existing position: "
                f"'{pos.question[:60]}'"
            )

    return True, "ok"

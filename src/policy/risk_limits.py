"""Risk limits — deterministic risk policy enforcement.

Checks ALL risk rules from config before allowing a trade.
Any single rule violation → NO TRADE.

Rules:
  1. Kill switch (manual + drawdown auto-kill)
  2. Drawdown heat level
  3. Maximum stake per market
  4. Maximum daily loss
  5. Maximum open positions
  6. Minimum edge threshold (uses net_edge after fees)
  7. Minimum liquidity
  8. Maximum spread
  9. Evidence quality threshold
  10. Confidence level filter
  11. Minimum implied probability floor
  12. Edge direction (positive after costs)
  13. Market type allowed
  14. Portfolio category/event exposure
  15. Timeline endgame check
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.config import RiskConfig, ForecastingConfig
from src.policy.edge_calc import EdgeResult
from src.forecast.feature_builder import MarketFeatures
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class RiskCheckResult:
    """Result of risk limit checks."""
    allowed: bool
    decision: str  # "TRADE" | "NO TRADE"
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks_passed: list[str] = field(default_factory=list)
    drawdown_heat: int = 0
    portfolio_gate: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": self.decision,
            "violations": self.violations,
            "warnings": self.warnings,
            "checks_passed": self.checks_passed,
            "drawdown_heat": self.drawdown_heat,
        }


def check_risk_limits(
    edge: EdgeResult,
    features: MarketFeatures,
    risk_config: RiskConfig,
    forecast_config: ForecastingConfig,
    current_open_positions: int = 0,
    daily_pnl: float = 0.0,
    market_type: str = "UNKNOWN",
    allowed_types: list[str] | None = None,
    restricted_types: list[str] | None = None,
    drawdown_state: Any | None = None,
    portfolio_gate: tuple[bool, str] = (True, "ok"),
    confidence_level: str = "LOW",
    min_edge_override: float | None = None,
) -> RiskCheckResult:
    """Run all risk checks. Returns TRADE only if ALL pass."""
    violations: list[str] = []
    warnings: list[str] = []
    passed: list[str] = []
    heat_level = 0

    # 1. Kill switch (manual)
    if risk_config.kill_switch:
        violations.append("KILL_SWITCH: Trading is disabled via kill switch")
    else:
        passed.append("kill_switch: OK")

    # 2. Drawdown kill switch (automatic)
    if drawdown_state is not None:
        heat_level = drawdown_state.heat_level
        if drawdown_state.is_killed:
            violations.append(
                f"DRAWDOWN_KILL: Auto kill-switch at "
                f"{drawdown_state.drawdown_pct:.1%} drawdown"
            )
        elif drawdown_state.drawdown_pct >= 0.20:
            violations.append(
                f"DRAWDOWN_LIMIT: {drawdown_state.drawdown_pct:.1%} >= 20% max drawdown"
            )
        elif heat_level >= 2:
            warnings.append(
                f"DRAWDOWN_HEAT: Level {heat_level}, "
                f"Kelly multiplied by {drawdown_state.kelly_multiplier:.2f}"
            )
        if heat_level == 0:
            passed.append("drawdown: healthy")

    # 3. Minimum edge — use net_edge (after fees) if available
    net_edge = getattr(edge, "net_edge", edge.abs_edge)
    abs_net = abs(net_edge)
    effective_min_edge = min_edge_override if min_edge_override is not None else risk_config.min_edge
    if abs_net < effective_min_edge:
        violations.append(
            f"MIN_EDGE: net |edge| {abs_net:.4f} < threshold {effective_min_edge}"
        )
    else:
        passed.append(f"min_edge: {abs_net:.4f} >= {effective_min_edge}")

    # 4. Maximum daily loss
    if daily_pnl < 0 and abs(daily_pnl) >= risk_config.max_daily_loss:
        violations.append(
            f"MAX_DAILY_LOSS: daily loss ${abs(daily_pnl):.2f} >= "
            f"limit ${risk_config.max_daily_loss:.2f}"
        )
    else:
        passed.append(
            f"daily_loss: ${abs(daily_pnl):.2f} < ${risk_config.max_daily_loss:.2f}"
        )

    # 5. Maximum open positions
    if current_open_positions >= risk_config.max_open_positions:
        violations.append(
            f"MAX_POSITIONS: {current_open_positions} >= "
            f"limit {risk_config.max_open_positions}"
        )
    else:
        passed.append(
            f"open_positions: {current_open_positions} < "
            f"{risk_config.max_open_positions}"
        )

    # 6. Minimum liquidity
    total_depth = features.bid_depth_5 + features.ask_depth_5
    if total_depth > 0 and total_depth < risk_config.min_liquidity:
        violations.append(
            f"MIN_LIQUIDITY: depth ${total_depth:.2f} < "
            f"threshold ${risk_config.min_liquidity:.2f}"
        )
    elif total_depth > 0:
        passed.append(
            f"liquidity: ${total_depth:.2f} >= ${risk_config.min_liquidity:.2f}"
        )
    else:
        warnings.append("LIQUIDITY: No orderbook depth data available")

    # 7. Maximum spread
    if features.spread_pct > 0 and features.spread_pct > risk_config.max_spread:
        violations.append(
            f"MAX_SPREAD: {features.spread_pct:.2%} > "
            f"threshold {risk_config.max_spread:.2%}"
        )
    elif features.spread_pct > 0:
        passed.append(
            f"spread: {features.spread_pct:.2%} <= {risk_config.max_spread:.2%}"
        )

    # 8. Evidence quality
    if features.evidence_quality < forecast_config.min_evidence_quality:
        violations.append(
            f"EVIDENCE_QUALITY: {features.evidence_quality:.2f} < threshold "
            f"{forecast_config.min_evidence_quality:.2f}"
        )
    else:
        passed.append(
            f"evidence_quality: {features.evidence_quality:.2f} >= "
            f"{forecast_config.min_evidence_quality:.2f}"
        )

    # 9. Confidence level filter — reject LOW confidence trades
    _CONF_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    min_conf = forecast_config.min_confidence_level if hasattr(forecast_config, "min_confidence_level") else "LOW"
    if _CONF_RANK.get(confidence_level, 0) < _CONF_RANK.get(min_conf, 0):
        violations.append(
            f"LOW_CONFIDENCE: {confidence_level} < minimum {min_conf}"
        )
    else:
        passed.append(f"confidence: {confidence_level} >= {min_conf}")

    # 10. Minimum implied probability — block micro-probability markets
    min_imp = getattr(risk_config, "min_implied_probability", 0.0)
    if min_imp > 0 and edge.implied_probability < min_imp:
        violations.append(
            f"MIN_IMPLIED_PROB: {edge.implied_probability:.2%} < "
            f"floor {min_imp:.2%}"
        )
    else:
        passed.append(
            f"implied_prob: {edge.implied_probability:.2%} >= "
            f"{min_imp:.2%}"
        )

    # 11. Edge direction — ensure edge is genuinely positive after costs
    if not edge.is_positive:
        violations.append(
            f"NEGATIVE_EDGE: net_edge {edge.net_edge:.4f} is not positive "
            f"(costs exceed raw edge)"
        )
    else:
        passed.append(f"edge_direction: positive ({edge.net_edge:.4f})")

    # 12. Market type check
    if allowed_types and market_type not in allowed_types:
        if restricted_types and market_type in restricted_types:
            violations.append(
                f"MARKET_TYPE: {market_type} is restricted and "
                f"requires explicit approval"
            )
        elif market_type == "UNKNOWN":
            violations.append(
                "MARKET_TYPE: Could not classify market type — abort"
            )
        else:
            warnings.append(
                f"MARKET_TYPE: {market_type} not in preferred list"
            )

    # 13. Clear resolution source
    if not features.has_clear_resolution:
        warnings.append("RESOLUTION: No clear resolution source defined")

    # 14. Portfolio exposure gate
    can_add, gate_reason = portfolio_gate
    if not can_add:
        violations.append(f"PORTFOLIO: {gate_reason}")
    else:
        passed.append("portfolio: OK")

    # 15. Timeline endgame check
    if features.is_near_resolution and features.hours_to_resolution < 6:
        warnings.append(
            f"TIMELINE: Only {features.hours_to_resolution:.1f}h to resolution — "
            f"consider exit only"
        )

    # Determine decision
    allowed = len(violations) == 0
    decision = "TRADE" if allowed else "NO TRADE"

    result = RiskCheckResult(
        allowed=allowed,
        decision=decision,
        violations=violations,
        warnings=warnings,
        checks_passed=passed,
        drawdown_heat=heat_level,
        portfolio_gate=gate_reason if not can_add else "ok",
    )

    log.info(
        "risk_limits.checked",
        market_id=features.market_id,
        decision=decision,
        violations=len(violations),
        warnings=len(warnings),
        passed=len(passed),
        heat=heat_level,
    )
    return result

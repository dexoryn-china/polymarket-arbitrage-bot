"""Tests for policy: edge calculation, risk limits, and position sizing."""

from __future__ import annotations

import pytest

from src.policy.edge_calc import calculate_edge, EdgeResult
from src.policy.risk_limits import check_risk_limits, RiskCheckResult
from src.policy.position_sizer import calculate_position_size, PositionSize
from src.config import RiskConfig, ForecastingConfig
from src.forecast.feature_builder import MarketFeatures


# ─── helpers ────────────────────────────────────────────────────────────

def _risk_cfg(**overrides) -> RiskConfig:
    """Return a RiskConfig with safe defaults."""
    defaults = dict(
        kill_switch=False,
        max_daily_loss=100.0,
        max_open_positions=20,
        max_stake_per_market=50.0,
        max_bankroll_fraction=0.05,
        min_edge=0.02,
        min_liquidity=500.0,
        max_spread=0.12,
        kelly_fraction=0.25,
        bankroll=5000.0,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def _forecast_cfg(**overrides) -> ForecastingConfig:
    defaults = dict(min_evidence_quality=0.3)
    defaults.update(overrides)
    return ForecastingConfig(**defaults)


def _features(**overrides) -> MarketFeatures:
    defaults = dict(
        market_id="m1",
        question="Test",
        market_type="MACRO",
        implied_probability=0.60,
        spread_pct=0.03,
        bid_depth_5=3000.0,
        ask_depth_5=2000.0,
        evidence_quality=0.8,
        has_clear_resolution=True,
    )
    defaults.update(overrides)
    return MarketFeatures(**defaults)


def _edge(implied: float = 0.60, model: float = 0.70) -> EdgeResult:
    return calculate_edge(implied_prob=implied, model_prob=model)


# ─── edge calculation ──────────────────────────────────────────────────

class TestEdgeCalc:
    def test_buy_yes_positive_edge(self) -> None:
        result = calculate_edge(implied_prob=0.60, model_prob=0.70)
        assert result.direction == "BUY_YES"
        assert result.raw_edge > 0
        # edge_pct = (0.70 - 0.60) / 0.60 ≈ 0.1667
        assert result.edge_pct == pytest.approx(0.1667, abs=0.01)

    def test_buy_no_positive_edge(self) -> None:
        result = calculate_edge(implied_prob=0.80, model_prob=0.20)
        assert result.direction == "BUY_NO"
        assert result.raw_edge < 0
        # Model says only 20% YES → buy NO
        assert result.expected_value_per_dollar > 0

    def test_no_edge(self) -> None:
        result = calculate_edge(implied_prob=0.60, model_prob=0.60)
        assert result.raw_edge == pytest.approx(0.0, abs=0.001)
        assert result.edge_pct == pytest.approx(0.0, abs=0.001)

    def test_slight_negative_edge_yes(self) -> None:
        result = calculate_edge(implied_prob=0.55, model_prob=0.50)
        # model < implied → raw_edge negative → BUY_NO direction
        assert result.direction == "BUY_NO"
        assert result.raw_edge < 0

    def test_extreme_high_model(self) -> None:
        result = calculate_edge(implied_prob=0.50, model_prob=0.95)
        assert result.direction == "BUY_YES"
        assert result.raw_edge == pytest.approx(0.45, abs=0.01)
        assert result.is_positive is True

    def test_abs_edge(self) -> None:
        result = calculate_edge(implied_prob=0.80, model_prob=0.30)
        assert result.abs_edge == pytest.approx(0.50, abs=0.01)

    def test_hold_to_resolution_single_fee(self) -> None:
        """Transaction cost should be single-leg (hold to resolution, no exit trade)."""
        result = calculate_edge(
            implied_prob=0.50, model_prob=0.60,
            transaction_fee_pct=0.02, gas_cost_usd=0.0,
        )
        # raw_edge = 0.10, cost = 0.02 (single leg), net_edge = 0.08
        assert result.abs_net_edge == pytest.approx(0.08, abs=0.005)

    def test_net_edge_not_double_counted(self) -> None:
        """Ensure cost is NOT doubled (old bug: cost * 2)."""
        result = calculate_edge(
            implied_prob=0.50, model_prob=0.55,
            transaction_fee_pct=0.02, gas_cost_usd=0.0,
        )
        # raw_edge = 0.05, cost = 0.02, net_edge = 0.03 (NOT 0.01)
        assert result.abs_net_edge == pytest.approx(0.03, abs=0.005)
        assert result.is_positive is True


# ─── risk limits ────────────────────────────────────────────────────────

class TestRiskLimits:
    def test_all_clear(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(),
            current_open_positions=3,
            daily_pnl=0.0,
            market_type="MACRO",
            confidence_level="MEDIUM",
        )
        assert result.allowed is True
        assert len(result.violations) == 0

    def test_kill_switch(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.90),
            features=_features(),
            risk_config=_risk_cfg(kill_switch=True),
            forecast_config=_forecast_cfg(),
        )
        assert result.allowed is False
        assert any("kill" in v.lower() for v in result.violations)

    def test_min_edge_violated(self) -> None:
        # Edge is 0.01, threshold is 0.05
        result = check_risk_limits(
            edge=_edge(0.60, 0.61),  # raw_edge=0.01, abs_edge=0.01
            features=_features(),
            risk_config=_risk_cfg(min_edge=0.05),
            forecast_config=_forecast_cfg(),
        )
        assert result.allowed is False
        assert any("edge" in v.lower() for v in result.violations)

    def test_max_daily_loss_exceeded(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(),
            risk_config=_risk_cfg(max_daily_loss=50.0),
            forecast_config=_forecast_cfg(),
            daily_pnl=-55.0,  # negative = loss
        )
        assert result.allowed is False
        assert any("daily" in v.lower() or "loss" in v.lower() for v in result.violations)

    def test_max_positions_exceeded(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(),
            risk_config=_risk_cfg(max_open_positions=5),
            forecast_config=_forecast_cfg(),
            current_open_positions=6,
        )
        assert result.allowed is False
        assert any("position" in v.lower() for v in result.violations)

    def test_low_liquidity(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(bid_depth_5=50.0, ask_depth_5=50.0),  # total=100
            risk_config=_risk_cfg(min_liquidity=500.0),
            forecast_config=_forecast_cfg(),
        )
        assert result.allowed is False
        assert any("liquidity" in v.lower() for v in result.violations)

    def test_wide_spread(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(spread_pct=0.15),  # 15%
            risk_config=_risk_cfg(max_spread=0.05),
            forecast_config=_forecast_cfg(),
        )
        assert result.allowed is False
        assert any("spread" in v.lower() for v in result.violations)

    def test_low_evidence_quality(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(evidence_quality=0.1),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(min_evidence_quality=0.5),
        )
        assert result.allowed is False
        assert any("evidence" in v.lower() for v in result.violations)

    def test_restricted_market_type(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(),
            market_type="SPORTS",
            allowed_types=["MACRO", "ELECTION"],
            restricted_types=["SPORTS"],
        )
        assert result.allowed is False
        assert any("market_type" in v.lower() or "restricted" in v.lower() for v in result.violations)

    def test_no_clear_resolution_is_warning(self) -> None:
        """Missing clear resolution is a warning, not a violation."""
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(has_clear_resolution=False),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(),
            confidence_level="MEDIUM",
        )
        # Still allowed, but a warning is emitted
        assert result.allowed is True
        assert any("resolution" in w.lower() for w in result.warnings)

    def test_multiple_violations(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.61),  # tiny edge
            features=_features(evidence_quality=0.05, spread_pct=0.20),
            risk_config=_risk_cfg(min_edge=0.05, max_spread=0.05),
            forecast_config=_forecast_cfg(min_evidence_quality=0.5),
            daily_pnl=-200.0,
        )
        assert result.allowed is False
        assert len(result.violations) >= 3


# ─── position sizing ────────────────────────────────────────────────────

class TestPositionSizer:
    def test_basic_kelly(self) -> None:
        edge = _edge(0.55, 0.70)
        ps = calculate_position_size(
            edge=edge,
            risk_config=_risk_cfg(
                bankroll=1000.0,
                kelly_fraction=0.25,
                max_stake_per_market=50.0,
                max_bankroll_fraction=0.05,
            ),
            confidence_level="HIGH",
        )
        assert isinstance(ps, PositionSize)
        assert ps.stake_usd > 0
        assert ps.stake_usd <= 50.0  # max_stake cap
        assert ps.stake_usd <= 1000.0 * 0.05  # max bankroll fraction

    def test_no_edge_no_bet(self) -> None:
        edge = calculate_edge(implied_prob=0.55, model_prob=0.55)
        ps = calculate_position_size(
            edge=edge,
            risk_config=_risk_cfg(bankroll=1000.0),
            confidence_level="HIGH",
        )
        # No edge → Kelly fraction is 0 → stake should be 0
        assert ps.stake_usd == 0.0

    def test_cap_at_max_stake(self) -> None:
        edge = _edge(0.50, 0.95)
        ps = calculate_position_size(
            edge=edge,
            risk_config=_risk_cfg(
                bankroll=100_000.0,
                kelly_fraction=0.5,
                max_stake_per_market=100.0,
                max_bankroll_fraction=0.1,
            ),
            confidence_level="HIGH",
        )
        assert ps.stake_usd <= 100.0

    def test_cap_at_bankroll_fraction(self) -> None:
        edge = _edge(0.50, 0.90)
        ps = calculate_position_size(
            edge=edge,
            risk_config=_risk_cfg(
                bankroll=200.0,
                kelly_fraction=0.5,
                max_stake_per_market=1000.0,
                max_bankroll_fraction=0.05,
            ),
            confidence_level="HIGH",
        )
        assert ps.stake_usd <= 200.0 * 0.05 + 0.01  # small float tolerance

    def test_low_confidence_reduces_size(self) -> None:
        edge = _edge(0.60, 0.80)
        rc = _risk_cfg(bankroll=1000.0, max_stake_per_market=500.0, max_bankroll_fraction=0.5)
        ps_high = calculate_position_size(edge=edge, risk_config=rc, confidence_level="HIGH")
        ps_low = calculate_position_size(edge=edge, risk_config=rc, confidence_level="LOW")
        # LOW confidence uses 0.5x Kelly multiplier vs HIGH uses full
        assert ps_low.stake_usd <= ps_high.stake_usd

    def test_capped_by_field(self) -> None:
        edge = _edge(0.50, 0.95)
        ps = calculate_position_size(
            edge=edge,
            risk_config=_risk_cfg(
                bankroll=100_000.0,
                max_stake_per_market=10.0,
                max_bankroll_fraction=0.5,
            ),
            confidence_level="HIGH",
        )
        assert ps.capped_by == "max_stake"

    def test_direction_matches_edge(self) -> None:
        edge_yes = _edge(0.50, 0.80)
        ps = calculate_position_size(edge=edge_yes, risk_config=_risk_cfg(), confidence_level="HIGH")
        assert ps.direction == "BUY_YES"

        edge_no = calculate_edge(implied_prob=0.80, model_prob=0.20)
        ps2 = calculate_position_size(edge=edge_no, risk_config=_risk_cfg(), confidence_level="HIGH")
        assert ps2.direction == "BUY_NO"


# ─── New Improvement Tests ──────────────────────────────────────────────


class TestConfidenceLevelFilter:
    """Improvement #1: Reject LOW confidence trades."""

    def test_low_confidence_rejected(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(min_confidence_level="MEDIUM"),
            confidence_level="LOW",
        )
        assert result.allowed is False
        assert any("LOW_CONFIDENCE" in v for v in result.violations)

    def test_medium_confidence_allowed_when_min_medium(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(min_confidence_level="MEDIUM"),
            confidence_level="MEDIUM",
        )
        assert not any("CONFIDENCE" in v for v in result.violations)

    def test_high_confidence_always_allowed(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(min_confidence_level="HIGH"),
            confidence_level="HIGH",
        )
        assert not any("CONFIDENCE" in v for v in result.violations)

    def test_medium_rejected_when_min_high(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(min_confidence_level="HIGH"),
            confidence_level="MEDIUM",
        )
        assert any("LOW_CONFIDENCE" in v for v in result.violations)

    def test_default_confidence_from_config(self) -> None:
        """Default ForecastingConfig should require MEDIUM."""
        from src.config import ForecastingConfig as FC
        fc = FC()
        assert fc.min_confidence_level == "MEDIUM"


class TestMinImpliedProbability:
    """Improvement #2: Block micro-probability markets (<10%)."""

    def test_micro_prob_blocked(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.05, 0.15),  # implied=5%
            features=_features(),
            risk_config=_risk_cfg(min_implied_probability=0.10),
            forecast_config=_forecast_cfg(),
            confidence_level="HIGH",
        )
        assert any("MIN_IMPLIED_PROB" in v for v in result.violations)

    def test_normal_prob_allowed(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(),
            risk_config=_risk_cfg(min_implied_probability=0.10),
            forecast_config=_forecast_cfg(),
            confidence_level="MEDIUM",
        )
        assert not any("IMPLIED_PROB" in v for v in result.violations)

    def test_boundary_10pct_allowed(self) -> None:
        """Exactly at 10% should pass (>=, not >)."""
        result = check_risk_limits(
            edge=_edge(0.10, 0.20),
            features=_features(),
            risk_config=_risk_cfg(min_implied_probability=0.10),
            forecast_config=_forecast_cfg(),
            confidence_level="MEDIUM",
        )
        assert not any("IMPLIED_PROB" in v for v in result.violations)

    def test_default_config_value(self) -> None:
        from src.config import RiskConfig as RC
        rc = RC()
        assert rc.min_implied_probability == 0.10


class TestEvidenceQualityThreshold:
    """Improvement #3: Raised min_evidence_quality to 0.55."""

    def test_default_raised_to_055(self) -> None:
        from src.config import ForecastingConfig as FC
        fc = FC()
        assert fc.min_evidence_quality == 0.55

    def test_050_evidence_rejected(self) -> None:
        result = check_risk_limits(
            edge=_edge(0.60, 0.70),
            features=_features(evidence_quality=0.50),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(min_evidence_quality=0.55),
            confidence_level="MEDIUM",
        )
        assert any("EVIDENCE_QUALITY" in v for v in result.violations)


class TestMaxStakeLowered:
    """Improvement #5: max_stake_per_market lowered to $50."""

    def test_default_is_50(self) -> None:
        from src.config import RiskConfig as RC
        rc = RC()
        assert rc.max_stake_per_market == 50.0

    def test_position_capped_at_50(self) -> None:
        edge = _edge(0.50, 0.95)  # huge edge
        ps = calculate_position_size(
            edge=edge,
            risk_config=_risk_cfg(
                bankroll=100_000.0,
                max_stake_per_market=50.0,
                max_bankroll_fraction=0.5,
            ),
            confidence_level="HIGH",
        )
        assert ps.stake_usd <= 50.0


class TestEdgeDirectionCheck:
    """Improvement #8: Reject trades where net_edge is negative (costs > raw edge)."""

    def test_negative_edge_rejected(self) -> None:
        """When costs exceed raw edge, is_positive=False → violation."""
        edge = calculate_edge(
            implied_prob=0.60, model_prob=0.62,  # raw_edge=0.02
            transaction_fee_pct=0.05,  # cost=0.05 > 0.02
        )
        assert edge.is_positive is False
        result = check_risk_limits(
            edge=edge,
            features=_features(),
            risk_config=_risk_cfg(min_edge=0.0),  # disable min_edge check
            forecast_config=_forecast_cfg(),
            confidence_level="MEDIUM",
        )
        assert any("NEGATIVE_EDGE" in v for v in result.violations)

    def test_positive_edge_passes(self) -> None:
        edge = _edge(0.60, 0.70)
        assert edge.is_positive is True
        result = check_risk_limits(
            edge=edge,
            features=_features(),
            risk_config=_risk_cfg(),
            forecast_config=_forecast_cfg(),
            confidence_level="MEDIUM",
        )
        assert not any("NEGATIVE_EDGE" in v for v in result.violations)


class TestEnsembleMinModels:
    """Improvement #9: min_models_required lowered to 1."""

    def test_default_is_1(self) -> None:
        from src.config import EnsembleConfig as EC
        ec = EC()
        assert ec.min_models_required == 1


class TestCategoryStakeMultipliers:
    """Improvement #7: Category-weighted stake sizing."""

    def test_category_multiplier_reduces_stake(self) -> None:
        """ELECTION category (0.5x) should produce smaller stake than MACRO (1.0x)."""
        edge = _edge(0.60, 0.80)
        rc = _risk_cfg(
            bankroll=5000.0, max_stake_per_market=5000.0,
            max_bankroll_fraction=0.99,
        )
        ps_macro = calculate_position_size(
            edge=edge, risk_config=rc, confidence_level="HIGH",
            category_multiplier=1.0,
        )
        ps_election = calculate_position_size(
            edge=edge, risk_config=rc, confidence_level="HIGH",
            category_multiplier=0.5,
        )
        assert ps_election.stake_usd < ps_macro.stake_usd
        # Without caps binding, the ratio should be exactly 0.5
        assert ps_election.stake_usd == pytest.approx(ps_macro.stake_usd * 0.5, rel=0.01)

    def test_category_multiplier_default_1(self) -> None:
        """Default category_multiplier should be 1.0 (no change)."""
        edge = _edge(0.60, 0.80)
        rc = _risk_cfg(bankroll=5000.0, max_stake_per_market=500.0, max_bankroll_fraction=0.5)
        ps_default = calculate_position_size(edge=edge, risk_config=rc, confidence_level="HIGH")
        ps_explicit = calculate_position_size(
            edge=edge, risk_config=rc, confidence_level="HIGH",
            category_multiplier=1.0,
        )
        assert ps_default.stake_usd == ps_explicit.stake_usd

    def test_config_has_category_multipliers(self) -> None:
        from src.config import RiskConfig as RC
        rc = RC()
        assert "MACRO" in rc.category_stake_multipliers
        assert rc.category_stake_multipliers["MACRO"] == 1.0
        assert rc.category_stake_multipliers["ELECTION"] == 0.50
        assert rc.category_stake_multipliers["CORPORATE"] == 0.75


class TestStopLossTakeProfit:
    """Improvement #6: Stop-loss / take-profit config defaults."""

    def test_config_defaults(self) -> None:
        from src.config import RiskConfig as RC
        rc = RC()
        assert rc.stop_loss_pct == 0.20
        assert rc.take_profit_pct == 0.30

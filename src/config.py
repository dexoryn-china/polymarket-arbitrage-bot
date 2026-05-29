"""Shared configuration loader and Pydantic settings.

Supports:
  - YAML file loading with env var overrides
  - Hot-reload via file watcher
  - All subsystem configs: scanning, research, forecasting, risk,
    execution, storage, observability, engine, alerts, drawdown,
    portfolio, cache, timeline, microstructure, ensemble
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, List

import yaml
from pydantic import BaseModel, Field, field_validator


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ScanningConfig(BaseModel):
    min_volume_usd: float = 1000
    min_liquidity_usd: float = 500
    max_spread: float = 0.08
    max_days_to_expiry: int = 120
    max_market_age_hours: float = 720.0  # 30 days — most Polymarket markets are older
    categories: list[str] = Field(default_factory=list)
    batch_size: int = 100
    preferred_types: list[str] = Field(default_factory=lambda: ["MACRO", "ELECTION", "CORPORATE", "LEGAL", "TECHNOLOGY", "SCIENCE"])
    restricted_types: list[str] = Field(default_factory=lambda: ["WEATHER"])
    # Pre-research filter settings
    filter_min_score: int = 25
    filter_blocked_types: list[str] = Field(default_factory=lambda: ["UNKNOWN"])
    research_cooldown_minutes: int = 60


class ResearchConfig(BaseModel):
    max_sources: int = 10
    source_timeout_secs: int = 15
    primary_domains: dict[str, list[str]] = Field(default_factory=dict)
    secondary_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    min_corroborating_sources: int = 2
    search_provider: str = "fallback"
    fetch_full_content: bool = True
    max_content_length: int = 15000
    content_fetch_top_n: int = 5
    stale_days_penalty_threshold: int = 7
    stale_days_heavy_penalty: int = 30


class ForecastingConfig(BaseModel):
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    calibration_method: str = "platt"
    low_evidence_penalty: float = 0.15
    min_evidence_quality: float = 0.55
    min_confidence_level: str = "MEDIUM"  # Reject LOW confidence trades


class EnsembleConfig(BaseModel):
    """Multi-model ensemble configuration."""
    enabled: bool = True
    models: list[str] = Field(default_factory=lambda: [
        "gpt-4o", "claude-3-5-sonnet-20241022", "gemini-1.5-pro"
    ])
    aggregation: str = "trimmed_mean"  # trimmed_mean | median | weighted
    trim_fraction: float = 0.1
    weights: dict[str, float] = Field(default_factory=lambda: {
        "gpt-4o": 0.40, "claude-3-5-sonnet-20241022": 0.35, "gemini-1.5-pro": 0.25
    })
    timeout_per_model_secs: int = 30
    min_models_required: int = 1
    fallback_model: str = "gpt-4o"


class RiskConfig(BaseModel):
    max_stake_per_market: float = 50.0
    max_daily_loss: float = 500.0
    max_open_positions: int = 25
    min_edge: float = 0.04
    min_liquidity: float = 2000
    min_volume: float = 1000
    max_spread: float = 0.06
    kelly_fraction: float = 0.25
    max_bankroll_fraction: float = 0.05
    kill_switch: bool = False
    bankroll: float = 5000.0
    transaction_fee_pct: float = 0.02
    exit_fee_pct: float = 0.02  # Fee for selling before resolution
    gas_cost_usd: float = 0.01
    min_implied_probability: float = 0.05  # Block micro-probability markets
    stop_loss_pct: float = 0.20  # Exit when position loses 20%
    take_profit_pct: float = 0.30  # Exit when position gains 30%
    max_holding_hours: float = 240.0  # Auto-exit positions held longer than this (10 days)
    min_stake_usd: float = 1.0  # Minimum stake to avoid dust trades
    # Volatility thresholds for position sizing
    volatility_high_threshold: float = 0.15
    volatility_med_threshold: float = 0.10
    volatility_high_min_mult: float = 0.4
    volatility_med_min_mult: float = 0.6
    category_stake_multipliers: dict[str, float] = Field(
        default_factory=lambda: {
            "MACRO": 1.0,
            "CORPORATE": 0.75,
            "ELECTION": 0.50,
        }
    )

    @field_validator("kelly_fraction")
    @classmethod
    def _clamp_kelly(cls, v: float) -> float:
        if v < 0 or v > 1.0:
            raise ValueError(f"kelly_fraction must be in [0, 1], got {v}")
        return v

    @field_validator("bankroll")
    @classmethod
    def _positive_bankroll(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"bankroll must be positive, got {v}")
        return v

    @field_validator("max_stake_per_market", "max_daily_loss")
    @classmethod
    def _positive_limits(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"value must be positive, got {v}")
        return v


class DrawdownConfig(BaseModel):
    """Drawdown management configuration."""
    enabled: bool = True
    max_drawdown_pct: float = 0.20
    warning_drawdown_pct: float = 0.10
    critical_drawdown_pct: float = 0.15
    auto_reduce_at_warning: float = 0.50
    auto_reduce_at_critical: float = 0.25
    auto_kill_at_max: bool = True
    heat_window_trades: int = 10
    heat_loss_streak_threshold: int = 3
    heat_reduction_factor: float = 0.50
    recovery_trades_required: int = 5
    snapshot_interval_minutes: int = 15


class PortfolioConfig(BaseModel):
    """Portfolio-level risk configuration."""
    max_category_exposure_pct: float = 0.35
    max_single_event_exposure_pct: float = 0.25
    max_correlated_positions: int = 4
    correlation_similarity_threshold: float = 0.7
    rebalance_check_interval_minutes: int = 30
    category_limits: dict[str, float] = Field(default_factory=lambda: {
        "MACRO": 0.40, "ELECTION": 0.35, "CORPORATE": 0.30,
        "WEATHER": 0.15, "SPORTS": 0.15,
    })


class TimelineConfig(BaseModel):
    """Resolution timeline configuration."""
    near_resolution_hours: int = 48
    near_resolution_confidence_boost: float = 0.15
    early_market_uncertainty_penalty: float = 0.10
    early_market_days_threshold: int = 60
    exit_before_resolution_hours: int = 0  # Disabled — hold through resolution
    time_decay_urgency_start_days: int = 7
    time_decay_max_multiplier: float = 1.5


class MicrostructureConfig(BaseModel):
    """Market microstructure analysis configuration."""
    whale_size_threshold_usd: float = 2000.0
    flow_imbalance_windows: list[int] = Field(default_factory=lambda: [60, 240, 1440])
    depth_change_alert_pct: float = 0.30
    trade_acceleration_window_mins: int = 30
    trade_acceleration_threshold: float = 2.0
    vwap_lookback_trades: int = 100


class ExecutionConfig(BaseModel):
    dry_run: bool = True
    default_order_type: str = "limit"
    slippage_tolerance: float = 0.01
    limit_order_ttl_secs: int = 300
    max_retries: int = 3
    retry_backoff_secs: float = 2.0
    twap_enabled: bool = True
    twap_num_slices: int = 5
    twap_interval_secs: int = 30
    adaptive_pricing: bool = True
    queue_position_target: str = "mid"
    max_market_impact_pct: float = 0.10
    stale_order_cancel_secs: int = 600
    iceberg_threshold_usd: float = 500.0
    iceberg_show_pct: float = 0.20


class StorageConfig(BaseModel):
    db_type: str = "sqlite"
    sqlite_path: str = "data/bot.db"


class CacheConfig(BaseModel):
    """Caching configuration."""
    enabled: bool = True
    search_ttl_secs: int = 3600
    orderbook_ttl_secs: int = 30
    llm_response_ttl_secs: int = 1800
    market_list_ttl_secs: int = 300
    max_cache_size_mb: int = 100


class ObservabilityConfig(BaseModel):
    log_level: str = "INFO"
    log_format: str = "json"
    log_file: str = "logs/bot.log"
    enable_metrics: bool = True
    reports_dir: str = "reports/"


class AlertsConfig(BaseModel):
    """Alerting configuration."""
    enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    slack_webhook_url: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_user: str = ""
    email_smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""
    alert_on_trade: bool = True
    alert_on_risk_breach: bool = True
    alert_on_drawdown_warning: bool = True
    alert_on_system_error: bool = True
    alert_on_kill_switch: bool = True
    daily_summary_enabled: bool = True
    daily_summary_hour: int = 18
    min_alert_interval_secs: int = 60
    min_alert_level: str = "info"  # info | warning | critical

    @property
    def telegram_token(self) -> str:
        return self.telegram_bot_token

    @property
    def discord_webhook(self) -> str:
        return self.discord_webhook_url

    @property
    def slack_webhook(self) -> str:
        return self.slack_webhook_url


class WalletScannerConfig(BaseModel):
    """Whale / smart-money wallet scanner configuration."""
    enabled: bool = True
    scan_interval_minutes: int = 15
    min_whale_count: int = 1       # single whale with big $ = valid signal
    min_conviction_score: float = 15.0
    max_wallets: int = 20          # max wallets to track
    conviction_edge_boost: float = 0.08  # boost edge by 8% when whales agree
    conviction_edge_penalty: float = 0.02  # penalise edge when whales disagree
    whale_convergence_min_edge: float = 0.02  # lower min_edge when whale+model agree
    track_leaderboard: bool = True  # auto-track leaderboard wallets
    custom_wallets: list[str] = Field(default_factory=list)  # user-added wallet addresses


class EngineConfig(BaseModel):
    """Main trading engine configuration."""
    scan_interval_minutes: int = 15
    research_interval_minutes: int = 30
    position_check_interval_minutes: int = 5
    max_concurrent_research: int = 3
    max_markets_per_cycle: int = 5
    auto_start: bool = False
    paper_mode: bool = True
    cycle_interval_secs: int = 300  # 5 minutes between full cycles


_SECRET_FIELDS = frozenset({
    "telegram_bot_token", "discord_webhook_url", "slack_webhook_url",
    "email_smtp_password",
})


class BotConfig(BaseModel):
    scanning: ScanningConfig = Field(default_factory=ScanningConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    forecasting: ForecastingConfig = Field(default_factory=ForecastingConfig)
    ensemble: EnsembleConfig = Field(default_factory=EnsembleConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    drawdown: DrawdownConfig = Field(default_factory=DrawdownConfig)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
    timeline: TimelineConfig = Field(default_factory=TimelineConfig)
    microstructure: MicrostructureConfig = Field(default_factory=MicrostructureConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    wallet_scanner: WalletScannerConfig = Field(default_factory=WalletScannerConfig)

    def redacted_dict(self) -> dict[str, Any]:
        """Return config dict with secret values masked."""
        raw = self.model_dump()
        _redact_secrets(raw)
        return raw


def _redact_secrets(d: dict[str, Any]) -> None:
    """Recursively mask secret fields in a dict."""
    for k, v in d.items():
        if isinstance(v, dict):
            _redact_secrets(v)
        elif k in _SECRET_FIELDS and isinstance(v, str) and v:
            d[k] = v[:3] + "***"


# Env var prefix → config field mapping for overrides
_ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    # ENV_VAR_NAME: (section, field, cast_type)
    "BOT_BANKROLL": ("risk", "bankroll", float),
    "BOT_MAX_STAKE": ("risk", "max_stake_per_market", float),
    "BOT_MAX_DAILY_LOSS": ("risk", "max_daily_loss", float),
    "BOT_KELLY_FRACTION": ("risk", "kelly_fraction", float),
    "BOT_DRY_RUN": ("execution", "dry_run", lambda v: v.lower() in ("true", "1", "yes")),
    "BOT_LOG_LEVEL": ("observability", "log_level", str),
    "BOT_SCAN_INTERVAL": ("engine", "scan_interval_minutes", int),
    "BOT_CYCLE_INTERVAL": ("engine", "cycle_interval_secs", int),
}


def _apply_env_overrides(raw: dict[str, Any]) -> None:
    """Apply environment variable overrides to raw config dict."""
    for env_var, (section, field_name, cast) in _ENV_OVERRIDES.items():
        val = os.environ.get(env_var)
        if val is not None:
            raw.setdefault(section, {})
            raw[section][field_name] = cast(val)  # type: ignore[operator]


def load_config(path: str | Path | None = None) -> BotConfig:
    """Load config from YAML file with env var overrides, falling back to defaults."""
    if path is None:
        path = _PROJECT_ROOT / "config.yaml"
    path = Path(path)
    if path.exists():
        with open(path) as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
    else:
        raw = {}
    _apply_env_overrides(raw)
    return BotConfig(**raw)


def is_live_trading_enabled() -> bool:
    """Check if live trading is explicitly enabled via env var."""
    return os.environ.get("ENABLE_LIVE_TRADING", "").lower() == "true"


class ConfigWatcher:
    """Watch config file for changes and hot-reload."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else _PROJECT_ROOT / "config.yaml"
        self._last_mtime: float = 0.0
        self._config: BotConfig = load_config(self._path)
        self._callbacks: List[Callable[[BotConfig], None]] = []
        self._update_mtime()

    def _update_mtime(self) -> None:
        if self._path.exists():
            self._last_mtime = self._path.stat().st_mtime

    @property
    def config(self) -> BotConfig:
        return self._config

    def on_change(self, callback: Callable[[BotConfig], None]) -> None:
        """Register a callback for config changes."""
        self._callbacks.append(callback)

    def check_and_reload(self) -> bool:
        """Check if config file changed and reload if so. Returns True if reloaded."""
        if not self._path.exists():
            return False
        current_mtime = self._path.stat().st_mtime
        if current_mtime > self._last_mtime:
            try:
                new_config = load_config(self._path)
                self._config = new_config
                self._last_mtime = current_mtime
                for cb in self._callbacks:
                    cb(new_config)
                return True
            except Exception:
                return False
        return False

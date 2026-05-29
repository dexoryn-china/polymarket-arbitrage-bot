"""Continuous trading loop — the brain of the bot.

Runs on a configurable cycle (default 5 minutes):
  1. Discover & filter markets
  2. Build features for each market
  3. Research top candidates (evidence gathering)
  4. Forecast probabilities
  5. Calculate edges
  6. Check risk limits
  7. Size positions
  8. Route orders (paper or live)
  9. Monitor existing positions for exits

Between cycles:
  - Check drawdown state
  - Monitor position exits (stop-loss, resolution/100%, time-based)
  - Persist engine state to DB for dashboard
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from src.config import BotConfig, load_config, is_live_trading_enabled
from src.policy.drawdown import DrawdownManager
from src.policy.portfolio_risk import PortfolioRiskManager, PositionSnapshot
from src.engine.market_filter import ResearchCache, filter_markets, FilterStats
from src.analytics.regime_detector import RegimeDetector, RegimeState
from src.analytics.calibration_feedback import CalibrationFeedbackLoop
from src.analytics.adaptive_weights import AdaptiveModelWeighter
from src.analytics.smart_entry import SmartEntryCalculator
from src.analytics.wallet_scanner import WalletScanner, save_scan_result
from src.connectors.ws_feed import WebSocketFeed, PriceTick
from src.observability.logger import get_logger
from src.observability.metrics import cost_tracker

log = get_logger(__name__)


@dataclass
class CycleResult:
    """Summary of one trading cycle."""
    cycle_id: int
    started_at: float
    ended_at: float = 0.0
    duration_secs: float = 0.0
    markets_scanned: int = 0
    markets_researched: int = 0
    edges_found: int = 0
    trades_attempted: int = 0
    trades_executed: int = 0
    errors: list[str] = field(default_factory=list)
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


@dataclass
class PipelineContext:
    """Carries state through the processing pipeline stages."""
    market: Any
    cycle_id: int
    market_id: str = ""
    question: str = ""
    classification: Any = None
    sources: list[Any] = field(default_factory=list)
    evidence: Any = None
    features: Any = None
    forecast: Any = None
    edge_result: Any = None
    has_edge: bool = False
    risk_result: Any = None
    position: Any = None
    whale_converged: bool = False   # True when whale signal agrees with model edge
    result: dict[str, Any] = field(default_factory=lambda: {
        "has_edge": False, "trade_attempted": False, "trade_executed": False,
    })


class TradingEngine:
    """Continuous trading engine that coordinates all bot components."""

    def __init__(self, config: Any | None = None):
        self.config: BotConfig = config or load_config()
        self._running = False
        self._cycle_count = 0
        self._cycle_history: list[CycleResult] = []

        bankroll = self.config.risk.bankroll
        self.drawdown = DrawdownManager(bankroll, self.config)
        self.portfolio = PortfolioRiskManager(bankroll, self.config)

        self._pre_cycle_hooks: list[Callable] = []
        self._post_cycle_hooks: list[Callable] = []
        self._positions: list[PositionSnapshot] = []

        # Pre-research filter
        self._research_cache = ResearchCache(
            cooldown_minutes=self.config.scanning.research_cooldown_minutes,
        )
        self._last_filter_stats: FilterStats | None = None

        # ── Analytics & Intelligence Layer ──
        self._regime_detector = RegimeDetector()
        self._calibration_loop = CalibrationFeedbackLoop()
        self._adaptive_weighter = AdaptiveModelWeighter(self.config.ensemble)
        self._smart_entry = SmartEntryCalculator()
        self._current_regime: RegimeState | None = None

        # ── Wallet / Whale Scanner ──
        self._wallet_scanner = WalletScanner(
            min_whale_count=self.config.wallet_scanner.min_whale_count,
            min_conviction_score=self.config.wallet_scanner.min_conviction_score,
        )
        self._last_wallet_scan: float = 0.0
        self._latest_scan_result: Any = None

        # ── WebSocket price feed ──
        self._ws_feed = WebSocketFeed()
        self._ws_task: asyncio.Task[None] | None = None

        # ── Rebalance / Arbitrage tracking ──
        self._last_rebalance_check: float = 0.0
        self._last_arbitrage_scan: float = 0.0
        self._latest_arb_opportunities: list[Any] = []

        # Database (initialised in start())
        self._db: Any = None
        self._audit: Any = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def cycle_history(self) -> list[CycleResult]:
        return list(self._cycle_history)

    def add_pre_cycle_hook(self, fn: Callable) -> None:
        self._pre_cycle_hooks.append(fn)

    def add_post_cycle_hook(self, fn: Callable) -> None:
        self._post_cycle_hooks.append(fn)

    # ── Lifecycle ────────────────────────────────────────────────────

    def _init_db(self) -> None:
        from src.storage.database import Database
        from src.storage.audit import AuditTrail
        self._db = Database(self.config.storage)
        self._db.connect()
        self._audit = AuditTrail()
        log.info("engine.db_connected", path=self.config.storage.sqlite_path)

    def _persist_engine_state(self, extra: dict[str, Any] | None = None) -> None:
        if not self._db:
            return
        try:
            state = {
                "running": self._running,
                "cycle_count": self._cycle_count,
                "live_trading": is_live_trading_enabled(),
                "paper_mode": self.config.engine.paper_mode,
                "last_cycle": (
                    self._cycle_history[-1].to_dict()
                    if self._cycle_history else None
                ),
                "positions": len(self._positions),
                "scan_interval_minutes": self.config.engine.scan_interval_minutes,
                "max_markets_per_cycle": self.config.engine.max_markets_per_cycle,
                "auto_start": self.config.engine.auto_start,
                "filter_stats": (
                    self._last_filter_stats.__dict__
                    if self._last_filter_stats else None
                ),
                "research_cache_size": self._research_cache.size(),
            }
            if extra:
                state.update(extra)
            self._db.set_engine_state("engine_status", json.dumps(state))
            dd = self.drawdown.state
            self._db.set_engine_state("drawdown", json.dumps(dd.to_dict()))
        except Exception as e:
            log.warning("engine.persist_state_error", error=str(e))

    async def start(self) -> None:
        self._running = True
        interval = self.config.engine.cycle_interval_secs
        self._init_db()
        self._db.insert_alert("info", "\U0001f916 Trading engine started", "system")
        log.info(
            "engine.starting",
            interval_secs=interval,
            live_trading=is_live_trading_enabled(),
            bankroll=self.config.risk.bankroll,
        )
        self._persist_engine_state()

        # Graceful shutdown on SIGTERM / SIGINT
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._handle_signal, sig)
            except (NotImplementedError, RuntimeError):
                pass  # Windows or non-main thread (e.g. dashboard)

        # Start WebSocket price feed in background
        try:
            self._ws_task = asyncio.create_task(self._ws_feed.start())
            log.info("engine.ws_feed_started")
        except Exception as e:
            log.warning("engine.ws_feed_start_error", error=str(e))

        while self._running:
            try:
                await self._run_cycle()
            except Exception as e:
                log.error("engine.cycle_error", error=str(e))
                traceback.print_exc()
                if self._db:
                    self._db.insert_alert("error", f"Cycle error: {e}", "system")
            self._persist_engine_state()
            if self._running:
                log.info("engine.sleeping", seconds=interval)
                await asyncio.sleep(interval)

        log.info("engine.stopped", total_cycles=self._cycle_count)
        if self._db:
            self._db.insert_alert("info", "\U0001f6d1 Trading engine stopped", "system")
            self._persist_engine_state({"running": False})

    def stop(self) -> None:
        log.info("engine.stop_requested")
        self._running = False
        # Stop WebSocket feed
        if self._ws_task and not self._ws_task.done():
            asyncio.ensure_future(self._ws_feed.stop())

    def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        log.info("engine.signal_received", signal=sig.name)
        self.stop()

    # ── Cycle ────────────────────────────────────────────────────────

    async def _run_cycle(self) -> CycleResult:
        self._cycle_count += 1
        cycle = CycleResult(cycle_id=self._cycle_count, started_at=time.time())
        log.info("engine.cycle_start", cycle_id=cycle.cycle_id)

        try:
            for hook in self._pre_cycle_hooks:
                try:
                    if asyncio.iscoroutinefunction(hook):
                        await hook()
                    else:
                        hook()
                except Exception as e:
                    log.warning("engine.hook_error", hook=str(hook), error=str(e))

            can_trade, dd_reason = self.drawdown.can_trade()
            if not can_trade:
                log.warning("engine.drawdown_halt", reason=dd_reason)
                cycle.status = "skipped"
                cycle.errors.append(f"Drawdown halt: {dd_reason}")
                if self._db:
                    self._db.insert_alert("warning", f"Cycle skipped: {dd_reason}", "risk")
                self._finish_cycle(cycle)
                return cycle

            # ── Regime Detection ─────────────────────────────────────
            try:
                if self._db:
                    self._current_regime = self._regime_detector.detect(
                        self._db.conn,
                    )
                    # Persist regime state for dashboard
                    import datetime as _dt
                    self._db.conn.execute("""
                        INSERT INTO regime_history
                            (regime, confidence, kelly_multiplier,
                             size_multiplier, explanation, detected_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        self._current_regime.regime,
                        self._current_regime.confidence,
                        self._current_regime.kelly_multiplier,
                        self._current_regime.size_multiplier,
                        self._current_regime.explanation,
                        _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    ))
                    self._db.conn.commit()
            except Exception as e:
                log.warning("engine.regime_detection_error", error=str(e))

            # ── Calibration check ────────────────────────────────────
            try:
                if self._db and self._cycle_count % 10 == 0:
                    self._calibration_loop.retrain_calibrator(self._db.conn)
            except Exception as e:
                log.warning("engine.calibration_retrain_error", error=str(e))
                return cycle

            markets = await self._discover_markets()
            cycle.markets_scanned = len(markets)

            if not markets:
                log.info("engine.no_markets")
                cycle.status = "completed"
                self._finish_cycle(cycle)
                return cycle

            # Pre-research filter — skip low-quality markets before SerpAPI
            blocked_types = set(self.config.scanning.filter_blocked_types)
            preferred_types = self.config.scanning.preferred_types or None
            min_score = self.config.scanning.filter_min_score
            max_per_cycle = self.config.engine.max_markets_per_cycle
            max_age_hours = self.config.scanning.max_market_age_hours

            self._research_cache.clear_stale()

            filtered, fstats = filter_markets(
                markets,
                min_score=min_score,
                max_pass=max_per_cycle,
                research_cache=self._research_cache,
                blocked_types=blocked_types,
                preferred_types=preferred_types,
                max_market_age_hours=max_age_hours,
            )
            self._last_filter_stats = fstats
            cycle.markets_researched = len(filtered)

            if not filtered:
                log.info("engine.all_filtered", stats=fstats.__dict__)
                cycle.status = "completed"
                self._finish_cycle(cycle)
                return cycle

            for candidate in filtered:
                try:
                    result = await self._process_candidate(candidate, cycle.cycle_id)
                    # Mark as researched so it's skipped for cooldown period
                    self._research_cache.mark_researched(
                        getattr(candidate, "id", ""),
                    )
                    if result.get("has_edge"):
                        cycle.edges_found += 1
                    if result.get("trade_attempted"):
                        cycle.trades_attempted += 1
                    if result.get("trade_executed"):
                        cycle.trades_executed += 1
                except Exception as e:
                    log.error(
                        "engine.candidate_error",
                        market_id=getattr(candidate, "id", "?"),
                        error=str(e),
                    )
                    cycle.errors.append(str(e))
                    traceback.print_exc()

            await self._check_positions()
            await self._maybe_rebalance()
            await self._maybe_scan_wallets()
            await self._maybe_scan_arbitrage(markets)
            cycle.status = "completed"

        except Exception as e:
            cycle.status = "error"
            cycle.errors.append(str(e))
            log.error("engine.cycle_failed", error=str(e))

        for hook in self._post_cycle_hooks:
            try:
                if asyncio.iscoroutinefunction(hook):
                    await hook()
                else:
                    hook()
            except Exception as e:
                log.warning("engine.hook_error", hook=str(hook), error=str(e))

        self._finish_cycle(cycle)
        return cycle

    def _finish_cycle(self, cycle: CycleResult) -> None:
        cycle.ended_at = time.time()
        cycle.duration_secs = round(cycle.ended_at - cycle.started_at, 2)
        self._cycle_history.append(cycle)
        if len(self._cycle_history) > 100:
            self._cycle_history = self._cycle_history[-50:]

        # Collect API cost summary for this cycle
        cycle_costs = cost_tracker.end_cycle()

        log.info(
            "engine.cycle_complete",
            cycle_id=cycle.cycle_id,
            duration=cycle.duration_secs,
            scanned=cycle.markets_scanned,
            researched=cycle.markets_researched,
            edges=cycle.edges_found,
            trades=cycle.trades_executed,
            status=cycle.status,
            cycle_cost_usd=cycle_costs["cycle_cost_usd"],
            total_cost_usd=cycle_costs["total_cost_usd"],
        )
        if self._db:
            self._db.insert_alert(
                "info",
                f"Cycle {cycle.cycle_id}: scanned={cycle.markets_scanned} "
                f"researched={cycle.markets_researched} edges={cycle.edges_found} "
                f"trades={cycle.trades_executed} ({cycle.duration_secs:.1f}s)",
                "engine",
            )

    # ── Market Discovery ─────────────────────────────────────────────

    async def _discover_markets(self) -> list[Any]:
        from src.connectors.polymarket_gamma import fetch_active_markets
        try:
            markets = await fetch_active_markets(
                min_volume=self.config.risk.min_liquidity, limit=200,
            )
            return markets
        except Exception as e:
            log.error("engine.discovery_error", error=str(e))
            return []

    async def _rank_markets(self, markets: list[Any]) -> list[Any]:
        scored = []
        for m in markets:
            score = (
                m.volume * 0.3
                + m.liquidity * 0.5
                + (1.0 if m.has_clear_resolution else 0.0) * 0.2
            )
            scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored]

    # ── Full Pipeline ────────────────────────────────────────────────

    async def _process_candidate(self, market: Any, cycle_id: int) -> dict[str, Any]:
        """Process a single market through the full research-to-trade pipeline.

        Orchestrates stages via PipelineContext and dedicated stage methods.
        """
        ctx = PipelineContext(market=market, cycle_id=cycle_id,
                              market_id=market.id, question=market.question)

        # ── Early exit: skip if we already hold a position ───────────
        if self._db:
            existing = [p for p in (self._db.get_open_positions())
                        if p.market_id == market.id]
            if existing:
                log.info("engine.duplicate_skip", market_id=market.id[:8],
                         msg="Already have open position — skipping")
                ctx.result["skipped"] = "duplicate_position"
                return ctx.result

        # ── Stage 0: Classification ──────────────────────────────────
        self._stage_classify(ctx)

        # ── Stage 1: Research ────────────────────────────────────────
        ok = await self._stage_research(ctx)
        if not ok:
            return ctx.result

        # ── Stage 2: Build Features ──────────────────────────────────
        from src.forecast.feature_builder import build_features
        ctx.features = build_features(market=market, evidence=ctx.evidence)

        # ── Stage 3: Forecast ────────────────────────────────────────
        await self._stage_forecast(ctx)

        # ── Stage 3b: Apply Calibration ──────────────────────────────
        self._stage_calibrate(ctx)

        # ── Stage 4: Edge Calculation + Whale Adjustment ─────────────
        self._stage_edge_calc(ctx)
        ctx.result["has_edge"] = ctx.has_edge

        # ── Stage 5: Risk Checks ─────────────────────────────────────
        self._stage_risk_checks(ctx)

        # ── Persist forecast to DB ───────────────────────────────────
        self._stage_persist_forecast(ctx)

        # ── Portfolio Correlation Check ──────────────────────────────
        self._stage_correlation_check(ctx)

        # ── Decision Gate ────────────────────────────────────────────
        if not ctx.risk_result.allowed:
            log.info("engine.no_trade", market_id=ctx.market_id,
                     violations=ctx.risk_result.violations)
            self._log_candidate(
                cycle_id, market, forecast=ctx.forecast, evidence=ctx.evidence,
                edge_result=ctx.edge_result, decision="NO TRADE",
                reason="; ".join(ctx.risk_result.violations),
            )
            if self._audit:
                self._audit.record_trade_decision(
                    market_id=ctx.market_id, question=ctx.question,
                    model_prob=ctx.forecast.model_probability,
                    implied_prob=ctx.forecast.implied_probability,
                    edge=ctx.forecast.edge,
                    confidence=ctx.forecast.confidence_level,
                    risk_result=ctx.risk_result.to_dict(), position_size=0.0,
                    evidence_summary=ctx.evidence.summary[:200],
                )
            return ctx.result

        # ── Stage 6: Position Sizing ─────────────────────────────────
        self._stage_position_sizing(ctx)
        if ctx.position is None:
            return ctx.result

        ctx.result["trade_attempted"] = True

        # ── Stage 7: Build & Route Order ─────────────────────────────
        await self._stage_execute_order(ctx)

        # ── Stage 8: Audit + Log ─────────────────────────────────────
        self._stage_audit_and_log(ctx)

        return ctx.result

    # ── Pipeline Stage Methods ────────────────────────────────────────

    def _stage_classify(self, ctx: PipelineContext) -> None:
        """Stage 0: Classify the market."""
        from src.engine.market_classifier import classify_and_log
        ctx.classification = classify_and_log(ctx.market)
        log.info(
            "engine.pipeline_start",
            market_id=ctx.market_id,
            question=ctx.question[:80],
            market_type=ctx.market.market_type,
            category=ctx.classification.category,
            subcategory=ctx.classification.subcategory,
            researchability=ctx.classification.researchability,
        )

    async def _stage_research(self, ctx: PipelineContext) -> bool:
        """Stage 1: Research. Returns False if research failed and pipeline should abort."""
        from src.research.query_builder import build_queries
        from src.research.source_fetcher import SourceFetcher
        from src.research.evidence_extractor import EvidenceExtractor
        from src.connectors.web_search import create_search_provider

        search_provider = create_search_provider(self.config.research.search_provider)
        source_fetcher = SourceFetcher(search_provider, self.config.research)

        try:
            max_q = ctx.classification.recommended_queries
            queries = build_queries(
                ctx.market, max_queries=max_q,
                category=ctx.classification.category,
                researchability=ctx.classification.researchability,
            )
            ctx.sources = await source_fetcher.fetch_sources(
                queries,
                market_type=ctx.classification.category or ctx.market.market_type,
                max_sources=self.config.research.max_sources,
            )
            extractor = EvidenceExtractor(self.config.forecasting)
            ctx.evidence = await extractor.extract(
                market_id=ctx.market_id, question=ctx.question,
                sources=ctx.sources, market_type=ctx.market.market_type,
            )
        except Exception as e:
            log.error("engine.research_failed", market_id=ctx.market_id, error=str(e))
            self._log_candidate(ctx.cycle_id, ctx.market, decision="SKIP",
                                reason=f"Research failed: {e}")
            return False
        finally:
            await source_fetcher.close()
            await search_provider.close()

        log.info(
            "engine.research_done", market_id=ctx.market_id,
            sources=len(ctx.sources), bullets=len(ctx.evidence.bullets),
            quality=round(ctx.evidence.quality_score, 3),
        )
        return True

    async def _stage_forecast(self, ctx: PipelineContext) -> None:
        """Stage 3: Run ensemble or single-model forecast."""
        if self.config.ensemble.enabled:
            from src.forecast.ensemble import EnsembleForecaster
            ens_forecaster = EnsembleForecaster(
                self.config.ensemble, self.config.forecasting,
            )
            # Inject learned adaptive weights if available
            try:
                if self._db:
                    cat = ctx.classification.category if ctx.classification else "UNKNOWN"
                    adaptive_result = self._adaptive_weighter.get_weights(
                        self._db.conn, cat,
                    )
                    if adaptive_result.data_available:
                        ens_forecaster.set_adaptive_weights(adaptive_result.weights)
                        log.info(
                            "engine.adaptive_weights_injected",
                            category=cat,
                            blend=round(adaptive_result.blend_factor, 3),
                            weights={k: round(v, 3)
                                     for k, v in adaptive_result.weights.items()},
                        )
            except Exception as e:
                log.warning("engine.adaptive_weights_inject_error", error=str(e))

            ens_result = await ens_forecaster.forecast(
                features=ctx.features, evidence=ctx.evidence,
            )
            from src.forecast.llm_forecaster import ForecastResult
            ctx.forecast = ForecastResult(
                market_id=ctx.market_id,
                question=ctx.question,
                market_type=ctx.market.market_type,
                resolution_source=ctx.market.resolution_source,
                implied_probability=ctx.features.implied_probability,
                model_probability=ens_result.model_probability,
                edge=ens_result.model_probability - ctx.features.implied_probability,
                confidence_level=ens_result.confidence_level,
                evidence=ens_result.key_evidence,
                invalidation_triggers=ens_result.invalidation_triggers,
                reasoning=ens_result.reasoning,
                evidence_quality=ctx.evidence.quality_score,
                num_sources=ctx.evidence.num_sources,
                raw_llm_response={
                    "ensemble": True,
                    "models_succeeded": ens_result.models_succeeded,
                    "models_failed": ens_result.models_failed,
                    "spread": ens_result.spread,
                    "agreement": ens_result.agreement_score,
                    "aggregation": ens_result.aggregation_method,
                },
            )
            # Apply low-evidence penalty
            if ctx.evidence.quality_score < self.config.forecasting.min_evidence_quality:
                penalty = self.config.forecasting.low_evidence_penalty
                old_prob = ctx.forecast.model_probability
                ctx.forecast.model_probability = old_prob * (1 - penalty) + 0.5 * penalty
                ctx.forecast.edge = ctx.forecast.model_probability - ctx.features.implied_probability
                log.info("engine.ensemble_low_evidence_penalty",
                         original=round(old_prob, 3),
                         adjusted=round(ctx.forecast.model_probability, 3))
        else:
            from src.forecast.llm_forecaster import LLMForecaster
            forecaster = LLMForecaster(self.config.forecasting)
            ctx.forecast = await forecaster.forecast(
                features=ctx.features, evidence=ctx.evidence,
                resolution_source=ctx.market.resolution_source,
            )

        log.info(
            "engine.forecast_done", market_id=ctx.market_id,
            implied=round(ctx.forecast.implied_probability, 3),
            model=round(ctx.forecast.model_probability, 3),
            edge=round(ctx.forecast.edge, 3),
            confidence=ctx.forecast.confidence_level,
        )

    def _stage_calibrate(self, ctx: PipelineContext) -> None:
        """Stage 3b: Apply probability calibration."""
        try:
            from src.forecast.calibrator import calibrate as apply_calibration
            ensemble_spread = 0.0
            if hasattr(ctx.forecast, "raw_llm_response") and isinstance(ctx.forecast.raw_llm_response, dict):
                ensemble_spread = ctx.forecast.raw_llm_response.get("spread", 0.0)
            cal_result = apply_calibration(
                raw_prob=ctx.forecast.model_probability,
                evidence_quality=ctx.evidence.quality_score,
                num_contradictions=(
                    len(ctx.evidence.contradictions)
                    if hasattr(ctx.evidence, "contradictions") else 0
                ),
                method=self.config.forecasting.calibration_method,
                low_evidence_penalty=self.config.forecasting.low_evidence_penalty,
                ensemble_spread=ensemble_spread,
            )
            if abs(cal_result.calibrated_probability - ctx.forecast.model_probability) > 0.005:
                log.info(
                    "engine.calibration_applied",
                    market_id=ctx.market_id,
                    raw=round(ctx.forecast.model_probability, 4),
                    calibrated=round(cal_result.calibrated_probability, 4),
                    adjustments=cal_result.adjustments,
                )
                ctx.forecast.model_probability = cal_result.calibrated_probability
                ctx.forecast.edge = ctx.forecast.model_probability - ctx.forecast.implied_probability
        except Exception as e:
            log.warning("engine.calibration_apply_error", error=str(e))

    def _stage_edge_calc(self, ctx: PipelineContext) -> None:
        """Stage 4: Edge calculation + whale/smart-money adjustment.

        Fixes applied:
          - Match by market_slug OR condition_id (not market_id)
          - Match direction BULLISH/BEARISH (not BUY/SELL)
          - Whale-edge convergence: when whale signal agrees with model edge,
            use a lower min_edge threshold for higher conviction trades
        """
        from src.policy.edge_calc import calculate_edge
        ctx.edge_result = calculate_edge(
            implied_prob=ctx.forecast.implied_probability,
            model_prob=ctx.forecast.model_probability,
            transaction_fee_pct=self.config.risk.transaction_fee_pct,
            gas_cost_usd=self.config.risk.gas_cost_usd,
        )

        # Track whale convergence for min_edge override later
        ctx_whale_converged = False

        # Whale / Smart-Money Edge Adjustment
        if (self.config.wallet_scanner.enabled
                and self._latest_scan_result
                and hasattr(self._latest_scan_result, "conviction_signals")):
            whale_cfg = self.config.wallet_scanner
            market_slug = getattr(ctx.market, "slug", "") or ""
            market_cid = getattr(ctx.market, "condition_id", "") or ""

            for sig in self._latest_scan_result.conviction_signals:
                # Match by slug, condition_id, or title substring
                sig_slug = getattr(sig, "market_slug", "") or ""
                sig_cid = getattr(sig, "condition_id", "") or ""
                matched = (
                    (sig_slug and market_slug and sig_slug == market_slug)
                    or (sig_cid and market_cid and sig_cid == market_cid)
                )
                if not matched:
                    continue

                # Direction matching: BULLISH→BUY_YES, BEARISH→BUY_NO
                whale_agrees = (
                    (sig.direction == "BULLISH" and ctx.edge_result.direction == "BUY_YES")
                    or (sig.direction == "BEARISH" and ctx.edge_result.direction == "BUY_NO")
                )
                if whale_agrees:
                    boost = whale_cfg.conviction_edge_boost
                    # Scale boost by conviction strength
                    strength_mult = (
                        1.5 if sig.signal_strength == "STRONG"
                        else 1.0 if sig.signal_strength == "MODERATE"
                        else 0.6
                    )
                    scaled_boost = boost * strength_mult
                    ctx.edge_result = calculate_edge(
                        implied_prob=ctx.forecast.implied_probability,
                        model_prob=(
                            min(0.99, ctx.forecast.model_probability + scaled_boost)
                            if ctx.edge_result.direction == "BUY_YES"
                            else max(0.01, ctx.forecast.model_probability - scaled_boost)
                        ),
                        transaction_fee_pct=self.config.risk.transaction_fee_pct,
                        gas_cost_usd=self.config.risk.gas_cost_usd,
                    )
                    ctx_whale_converged = True
                    ctx.whale_converged = True
                    log.info("engine.whale_edge_boost", market_id=ctx.market_id,
                             boost=round(scaled_boost, 4),
                             strength=sig.signal_strength,
                             whale_count=sig.whale_count,
                             new_edge=round(ctx.edge_result.abs_net_edge, 4))
                else:
                    penalty = whale_cfg.conviction_edge_penalty
                    ctx.edge_result = calculate_edge(
                        implied_prob=ctx.forecast.implied_probability,
                        model_prob=(
                            max(0.01, ctx.forecast.model_probability - penalty)
                            if ctx.edge_result.direction == "BUY_YES"
                            else min(0.99, ctx.forecast.model_probability + penalty)
                        ),
                        transaction_fee_pct=self.config.risk.transaction_fee_pct,
                        gas_cost_usd=self.config.risk.gas_cost_usd,
                    )
                    log.info("engine.whale_edge_penalty", market_id=ctx.market_id,
                             penalty=penalty, new_edge=round(ctx.edge_result.abs_net_edge, 4))
                break  # only apply first matching signal

        # Determine if we have edge — use lower threshold when whales agree
        min_edge = self.config.risk.min_edge
        if ctx_whale_converged:
            min_edge = self.config.wallet_scanner.whale_convergence_min_edge
            log.info("engine.whale_convergence",
                     market_id=ctx.market_id,
                     normal_min_edge=self.config.risk.min_edge,
                     whale_min_edge=min_edge,
                     edge=round(ctx.edge_result.abs_net_edge, 4))

        ctx.has_edge = (
            ctx.edge_result.is_positive
            and ctx.edge_result.abs_net_edge >= min_edge
        )

    def _stage_risk_checks(self, ctx: PipelineContext) -> None:
        """Stage 5: Risk limit checks."""
        from src.policy.risk_limits import check_risk_limits
        daily_pnl = self._db.get_daily_pnl() if self._db else 0.0
        open_positions = self._db.get_open_positions_count() if self._db else 0

        # When whales agree with our model, use a lower min_edge threshold
        whale_min_edge = None
        if ctx.whale_converged:
            whale_min_edge = self.config.wallet_scanner.whale_convergence_min_edge

        ctx.risk_result = check_risk_limits(
            edge=ctx.edge_result, features=ctx.features,
            risk_config=self.config.risk,
            forecast_config=self.config.forecasting,
            current_open_positions=open_positions,
            daily_pnl=daily_pnl,
            market_type=ctx.market.market_type,
            allowed_types=self.config.scanning.preferred_types or None,
            restricted_types=self.config.scanning.restricted_types or None,
            drawdown_state=self.drawdown.state,
            confidence_level=ctx.forecast.confidence_level if ctx.forecast else "LOW",
            min_edge_override=whale_min_edge,
        )

    def _stage_persist_forecast(self, ctx: PipelineContext) -> None:
        """Persist forecast and market records to DB."""
        if not self._db:
            return
        from src.storage.models import ForecastRecord, MarketRecord
        self._db.upsert_market(MarketRecord(
            id=ctx.market_id, condition_id=ctx.market.condition_id,
            question=ctx.question, market_type=ctx.market.market_type,
            category=ctx.market.category, volume=ctx.market.volume,
            liquidity=ctx.market.liquidity,
            end_date=ctx.market.end_date.isoformat() if ctx.market.end_date else "",
            resolution_source=ctx.market.resolution_source,
        ))
        self._db.insert_forecast(ForecastRecord(
            id=str(uuid.uuid4()), market_id=ctx.market_id,
            question=ctx.question, market_type=ctx.market.market_type,
            implied_probability=ctx.forecast.implied_probability,
            model_probability=ctx.forecast.model_probability,
            edge=ctx.forecast.edge,
            confidence_level=ctx.forecast.confidence_level,
            evidence_quality=ctx.evidence.quality_score,
            num_sources=ctx.evidence.num_sources,
            decision=ctx.risk_result.decision,
            reasoning=ctx.forecast.reasoning[:500],
            evidence_json=json.dumps(ctx.forecast.evidence[:5]),
            invalidation_triggers_json=json.dumps(ctx.forecast.invalidation_triggers),
            research_evidence_json=json.dumps({
                **ctx.evidence.to_dict(),
                "classification": ctx.classification.to_dict(),
            }),
        ))

    def _stage_correlation_check(self, ctx: PipelineContext) -> None:
        """Check portfolio correlation before allowing entry."""
        if not self._positions or not ctx.risk_result.allowed:
            return
        from src.policy.portfolio_risk import check_correlation
        corr_ok, corr_reason = check_correlation(
            existing_positions=self._positions,
            new_question=ctx.question,
            new_category=ctx.classification.category if ctx.classification else "",
            new_event_slug=ctx.market.slug or "",
            similarity_threshold=self.config.portfolio.correlation_similarity_threshold,
        )
        if not corr_ok:
            ctx.risk_result.allowed = False
            ctx.risk_result.violations.append(f"Correlation: {corr_reason}")
            log.info("engine.correlation_blocked",
                     market_id=ctx.market_id, reason=corr_reason)

    def _stage_position_sizing(self, ctx: PipelineContext) -> None:
        """Stage 6: Calculate position size. Sets ctx.position to None if too small."""
        from src.policy.position_sizer import calculate_position_size
        regime_kelly = (
            self._current_regime.kelly_multiplier
            if self._current_regime else 1.0
        )
        regime_size = (
            self._current_regime.size_multiplier
            if self._current_regime else 1.0
        )
        # Category-weighted stake multiplier
        category = (ctx.classification.category
                    if ctx.classification else ctx.market.category or "")
        cat_mults = getattr(self.config.risk, "category_stake_multipliers", {})
        cat_mult = cat_mults.get(category, 1.0)
        ctx.position = calculate_position_size(
            edge=ctx.edge_result, risk_config=self.config.risk,
            confidence_level=ctx.forecast.confidence_level,
            drawdown_multiplier=self.drawdown.state.kelly_multiplier,
            timeline_multiplier=ctx.features.time_decay_multiplier,
            price_volatility=ctx.features.price_volatility,
            regime_multiplier=regime_kelly * regime_size,
            category_multiplier=cat_mult,
        )
        if ctx.position.stake_usd < 1.0:
            log.info("engine.stake_too_small", market_id=ctx.market_id,
                     stake=ctx.position.stake_usd)
            self._log_candidate(
                ctx.cycle_id, ctx.market, forecast=ctx.forecast,
                evidence=ctx.evidence, edge_result=ctx.edge_result,
                decision="NO TRADE", reason="Stake too small",
                stake=ctx.position.stake_usd,
            )
            ctx.position = None

    async def _stage_execute_order(self, ctx: PipelineContext) -> None:
        """Stage 7: Build and route orders."""
        from src.execution.order_builder import build_order
        from src.execution.order_router import OrderRouter
        from src.connectors.polymarket_clob import CLOBClient

        market = ctx.market
        forecast = ctx.forecast
        edge_result = ctx.edge_result
        position = ctx.position

        yes_tokens = [t for t in market.tokens if t.outcome.lower() == "yes"]
        no_tokens = [t for t in market.tokens if t.outcome.lower() == "no"]
        if not yes_tokens and not no_tokens and len(market.tokens) >= 2:
            yes_tokens = [market.tokens[0]]
            no_tokens = [market.tokens[1]]
        elif not yes_tokens and market.tokens:
            yes_tokens = [market.tokens[0]]

        if edge_result.direction == "BUY_YES" and yes_tokens:
            token_id = yes_tokens[0].token_id
            implied_price = yes_tokens[0].price or forecast.implied_probability
        elif edge_result.direction == "BUY_NO" and no_tokens:
            token_id = no_tokens[0].token_id
            implied_price = no_tokens[0].price or (1 - forecast.implied_probability)
        else:
            token_id = (yes_tokens[0].token_id if yes_tokens
                        else (no_tokens[0].token_id if no_tokens else ""))
            implied_price = forecast.implied_probability

        if not token_id:
            log.warning("engine.no_token_id", market_id=ctx.market_id)
            self._log_candidate(
                ctx.cycle_id, market, forecast=forecast, evidence=ctx.evidence,
                edge_result=edge_result, decision="NO TRADE",
                reason="No token ID available",
            )
            return

        # Smart Entry: Calculate optimal entry price
        execution_strategy = "simple"
        try:
            regime_patience = (
                self._current_regime.entry_patience
                if self._current_regime else 1.0
            )
            entry_plan = self._smart_entry.calculate_entry(
                market_id=ctx.market_id,
                side=edge_result.direction,
                current_price=implied_price,
                fair_value=forecast.model_probability,
                edge=edge_result.abs_net_edge,
                spread=getattr(ctx.features, "spread", 0.0),
                hours_to_resolution=getattr(ctx.features, "hours_to_resolution", 720.0),
                regime_patience=regime_patience,
            )
            if entry_plan and entry_plan.recommended_price > 0:
                old_price = implied_price
                implied_price = entry_plan.recommended_price
                if entry_plan.recommended_strategy == "twap":
                    execution_strategy = "twap"
                log.info(
                    "engine.smart_entry", market_id=ctx.market_id,
                    old_price=round(old_price, 4),
                    new_price=round(implied_price, 4),
                    strategy=entry_plan.recommended_strategy,
                    improvement_bps=round(entry_plan.expected_improvement_bps, 1),
                )
        except Exception as e:
            log.warning("engine.smart_entry_error", error=str(e))

        orders = build_order(
            market_id=ctx.market_id, token_id=token_id,
            position=position, implied_price=implied_price,
            config=self.config.execution, execution_strategy=execution_strategy,
        )

        clob = CLOBClient()
        router = OrderRouter(clob, self.config.execution)
        ctx._order_statuses = []  # list[str]
        ctx._token_id = token_id
        try:
            for order in orders:
                order_result = await router.submit_order(order)
                ctx._order_statuses.append(order_result.status)
                log.info(
                    "engine.order_result", market_id=ctx.market_id,
                    order_id=order_result.order_id[:8],
                    status=order_result.status,
                    fill_price=order_result.fill_price,
                    fill_size=order_result.fill_size,
                )
                if self._db:
                    from src.storage.models import TradeRecord, PositionRecord
                    self._db.insert_trade(TradeRecord(
                        id=str(uuid.uuid4()),
                        order_id=order_result.order_id,
                        market_id=ctx.market_id, token_id=token_id,
                        side=edge_result.direction,
                        price=order_result.fill_price,
                        size=order_result.fill_size,
                        stake_usd=position.stake_usd,
                        status=order_result.status.upper(),
                        dry_run=order_result.status == "simulated",
                    ))
                    self._db.upsert_position(PositionRecord(
                        market_id=ctx.market_id, token_id=token_id,
                        direction=edge_result.direction,
                        entry_price=order_result.fill_price,
                        size=order_result.fill_size,
                        stake_usd=position.stake_usd,
                        current_price=order_result.fill_price, pnl=0.0,
                        question=ctx.question[:200] if ctx.question else "",
                        market_type=getattr(ctx.market, "market_type", ""),
                    ))
                ctx.result["trade_executed"] = True
                # Subscribe token to WebSocket feed for live pricing
                self._ws_feed.subscribe(token_id)
        finally:
            await clob.close()

    def _stage_audit_and_log(self, ctx: PipelineContext) -> None:
        """Stage 8: Audit trail + logging + adaptive weight recording."""
        order_statuses = getattr(ctx, "_order_statuses", [])
        token_id = getattr(ctx, "_token_id", "")

        if self._audit:
            self._audit.record_trade_decision(
                market_id=ctx.market_id, question=ctx.question,
                model_prob=ctx.forecast.model_probability,
                implied_prob=ctx.forecast.implied_probability,
                edge=ctx.forecast.edge,
                confidence=ctx.forecast.confidence_level,
                risk_result=ctx.risk_result.to_dict(),
                position_size=ctx.position.stake_usd if ctx.position else 0.0,
                order_id="",
                evidence_summary=ctx.evidence.summary[:200],
            )

        self._log_candidate(
            ctx.cycle_id, ctx.market, forecast=ctx.forecast,
            evidence=ctx.evidence, edge_result=ctx.edge_result,
            decision="TRADE", reason="All checks passed",
            stake=ctx.position.stake_usd if ctx.position else 0.0,
            order_status=order_statuses[0] if order_statuses else "",
        )
        if self._db:
            mode = (
                "\U0001f9ea Paper"
                if order_statuses and order_statuses[0] == "simulated"
                else "\U0001f4b0 Live"
            )
            self._db.insert_alert(
                "info",
                f'{mode} trade: {ctx.edge_result.direction} on '
                f'"{ctx.question[:60]}" '
                f"\u2014 stake ${ctx.position.stake_usd:.2f}, "
                f"edge {ctx.forecast.edge:+.3f}, "
                f"confidence {ctx.forecast.confidence_level}",
                "trade", ctx.market_id,
            )
        log.info(
            "engine.trade_executed", market_id=ctx.market_id,
            direction=ctx.edge_result.direction,
            stake=ctx.position.stake_usd if ctx.position else 0.0,
            edge=round(ctx.forecast.edge, 3), status=order_statuses,
        )

        # Record for adaptive weighting (model accuracy log)
        try:
            if self._db and hasattr(ctx.forecast, 'model_forecasts'):
                for model_name, prob in (ctx.forecast.model_forecasts or {}).items():
                    self._db.conn.execute("""
                        INSERT INTO model_forecast_log
                            (model_name, market_id, category, forecast_prob,
                             actual_outcome, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        model_name, ctx.market_id,
                        ctx.classification.category if ctx.classification else "UNKNOWN",
                        prob, -1.0,
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    ))
                    self._db.conn.commit()
        except Exception as e:
            log.warning("engine.model_forecast_log_error", error=str(e))

    def _log_candidate(
        self, cycle_id: int, market: Any,
        forecast: Any = None, evidence: Any = None,
        edge_result: Any = None,
        decision: str = "SKIP", reason: str = "",
        stake: float = 0.0, order_status: str = "",
    ) -> None:
        if not self._db:
            return
        try:
            self._db.insert_candidate(
                cycle_id=cycle_id, market_id=market.id,
                question=market.question[:200],
                market_type=market.market_type,
                implied_prob=(getattr(forecast, "implied_probability", market.best_bid)
                              if forecast else market.best_bid),
                model_prob=(getattr(forecast, "model_probability", 0.0)
                            if forecast else 0.0),
                edge=getattr(forecast, "edge", 0.0) if forecast else 0.0,
                evidence_quality=(getattr(evidence, "quality_score", 0.0)
                                  if evidence else 0.0),
                num_sources=(getattr(evidence, "num_sources", 0)
                             if evidence else 0),
                confidence=(getattr(forecast, "confidence_level", "")
                            if forecast else ""),
                decision=decision, decision_reasons=reason[:300],
                stake_usd=stake, order_status=order_status,
            )
        except Exception as e:
            log.warning("engine.log_candidate_error", error=str(e))

    def _record_performance_log(
        self,
        pos: Any,
        exit_price: float,
        pnl: float,
        mkt_record: Any = None,
    ) -> None:
        """Write a closed position to the performance_log table for analytics.

        Gathers forecast data (probability, edge, confidence, evidence quality)
        from the forecasts table and computes holding duration.
        """
        if not self._db:
            return
        try:
            import datetime as _dt
            from src.storage.models import PerformanceLogRecord

            # Look up the most recent forecast for this market
            forecasts = self._db.get_forecasts(market_id=pos.market_id, limit=1)
            fc = forecasts[0] if forecasts else None

            # Compute holding hours
            holding_hours = 0.0
            try:
                opened = _dt.datetime.fromisoformat(
                    pos.opened_at.replace("Z", "+00:00")
                )
                now = _dt.datetime.now(_dt.timezone.utc)
                holding_hours = (now - opened).total_seconds() / 3600
            except Exception:
                pass

            # Determine category from market record or forecast
            category = "UNKNOWN"
            if mkt_record and getattr(mkt_record, "category", None):
                category = mkt_record.category
            elif fc and getattr(fc, "market_type", None):
                category = fc.market_type

            # Determine actual outcome from exit price (for resolved markets)
            actual_outcome = None
            if exit_price >= 0.98:
                actual_outcome = 1.0
            elif exit_price <= 0.02:
                actual_outcome = 0.0

            self._db.insert_performance_log(PerformanceLogRecord(
                market_id=pos.market_id,
                question=(
                    getattr(mkt_record, "question", "")
                    if mkt_record else getattr(pos, "question", "")
                ),
                category=category,
                forecast_prob=fc.model_probability if fc else 0.0,
                actual_outcome=actual_outcome,
                edge_at_entry=fc.edge if fc else 0.0,
                confidence=fc.confidence_level if fc else "LOW",
                evidence_quality=fc.evidence_quality if fc else 0.0,
                stake_usd=pos.stake_usd,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                pnl=pnl,
                holding_hours=round(holding_hours, 2),
            ))

            log.info(
                "engine.performance_log_recorded",
                market_id=pos.market_id[:8],
                pnl=pnl,
                holding_hours=round(holding_hours, 1),
                category=category,
            )
        except Exception as e:
            log.warning("engine.performance_log_error", error=str(e))

    async def _check_positions(self) -> None:
        """Fetch live prices for all open positions and update PNL.

        Uses WebSocket feed when available for instant pricing,
        falls back to Gamma REST API when WS prices are stale or unavailable.
        """
        if not self._db:
            return

        positions = self._db.get_open_positions()
        if not positions:
            self._positions = []
            return

        from src.connectors.polymarket_gamma import GammaClient

        client = GammaClient()
        snapshots: list[PositionSnapshot] = []
        ws_hits = 0
        rest_hits = 0
        try:
            for pos in positions:
                try:
                    # Try WebSocket feed first for instant pricing
                    current_price = None
                    ws_tick = self._ws_feed.get_last_price(pos.token_id)
                    if ws_tick and (time.time() - ws_tick.timestamp) < 60:
                        current_price = ws_tick.mid or ws_tick.best_bid
                        ws_hits += 1
                    
                    # Fall back to REST API if WS price unavailable or stale
                    market = None
                    if current_price is None:
                        market = await client.get_market(pos.market_id)
                        current_price = pos.current_price  # fallback
                        for tok in market.tokens:
                            if tok.token_id == pos.token_id:
                                current_price = tok.price
                                break
                        rest_hits += 1

                    # Calculate PNL based on direction
                    if pos.direction in ("BUY_YES", "BUY"):
                        pnl = (current_price - pos.entry_price) * pos.size
                    elif pos.direction in ("BUY_NO", "SELL"):
                        pnl = (pos.entry_price - current_price) * pos.size
                    else:
                        pnl = (current_price - pos.entry_price) * pos.size

                    self._db.update_position_price(
                        pos.market_id, current_price, round(pnl, 4),
                    )

                    # Fetch market metadata (needed for snapshots + exit trades)
                    if market is None:
                        market = await client.get_market(pos.market_id)
                    mkt_record = self._db.get_market(pos.market_id)

                    # ── Determine exit reason (if any) ───────────────
                    sl_pct = getattr(self.config.risk, "stop_loss_pct", 0.0)
                    tp_pct = getattr(self.config.risk, "take_profit_pct", 0.0)
                    max_hold = getattr(self.config.risk, "max_holding_hours", 72.0)
                    pnl_pct = pnl / pos.stake_usd if pos.stake_usd > 0 else 0.0
                    exit_reason = ""

                    # Stop-loss
                    if sl_pct > 0 and pnl_pct <= -sl_pct:
                        exit_reason = f"STOP_LOSS: {pnl_pct:.1%} <= -{sl_pct:.0%}"
                    # Take-profit
                    elif tp_pct > 0 and pnl_pct >= tp_pct:
                        exit_reason = f"TAKE_PROFIT: {pnl_pct:.1%} >= +{tp_pct:.0%}"
                    # Market resolved (price at 0 or 1)
                    elif current_price is not None and (current_price >= 0.98 or current_price <= 0.02):
                        exit_reason = f"MARKET_RESOLVED: price={current_price:.4f}"
                    # Max holding period exceeded
                    elif max_hold > 0:
                        try:
                            import datetime as _dt
                            opened = _dt.datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
                            now = _dt.datetime.now(_dt.timezone.utc)
                            holding_hours = (now - opened).total_seconds() / 3600
                            if holding_hours >= max_hold:
                                exit_reason = f"MAX_HOLDING: {holding_hours:.1f}h >= {max_hold:.0f}h"
                        except Exception:
                            pass

                    if exit_reason:
                        log.info(
                            "engine.auto_exit",
                            market_id=pos.market_id[:8],
                            reason=exit_reason,
                            pnl=round(pnl, 4),
                            pnl_pct=f"{pnl_pct:.1%}",
                        )

                        # ── Record the exit trade ────────────────────
                        from src.storage.models import TradeRecord
                        self._db.insert_trade(TradeRecord(
                            id=f"exit-{pos.market_id[:8]}-{int(time.time())}",
                            order_id=f"auto-exit-{pos.market_id[:8]}",
                            market_id=pos.market_id,
                            token_id=pos.token_id,
                            side="SELL",
                            price=current_price,
                            size=pos.size,
                            stake_usd=pos.stake_usd,
                            status=f"SIMULATED|{exit_reason}",
                            dry_run=True,
                        ))

                        # ── Archive position before deletion ─────────
                        self._db.archive_position(
                            pos=pos,
                            exit_price=current_price,
                            pnl=round(pnl, 4),
                            close_reason=exit_reason.split(":")[0],
                        )

                        # ── Write to performance_log for analytics ───
                        self._record_performance_log(
                            pos=pos,
                            exit_price=current_price,
                            pnl=round(pnl, 4),
                            mkt_record=mkt_record,
                        )

                        self._db.remove_position(pos.market_id)
                        self._db.insert_alert(
                            "warning",
                            f"Auto-exit {pos.market_id[:8]}: {exit_reason} "
                            f"(PNL ${pnl:.2f})",
                            "engine",
                        )
                        continue  # skip snapshot — position closed

                    # Build snapshot for portfolio risk
                    snapshots.append(PositionSnapshot(
                        market_id=pos.market_id,
                        question=mkt_record.question if mkt_record else "",
                        category=mkt_record.category if mkt_record else "",
                        event_slug=market.slug or "",
                        side="YES" if pos.direction in ("BUY_YES", "BUY") else "NO",
                        size_usd=pos.stake_usd,
                        entry_price=pos.entry_price,
                        current_price=current_price,
                        unrealised_pnl=round(pnl, 4),
                    ))

                    log.info(
                        "engine.position_update",
                        market_id=pos.market_id[:8],
                        entry=pos.entry_price,
                        current=current_price,
                        pnl=round(pnl, 4),
                        source="ws" if ws_tick else "rest",
                    )

                except Exception as e:
                    log.warning(
                        "engine.position_price_error",
                        market_id=pos.market_id[:8],
                        error=str(e),
                    )
                    # Keep stale snapshot
                    snapshots.append(PositionSnapshot(
                        market_id=pos.market_id,
                        question="",
                        category="",
                        event_slug="",
                        side="YES" if pos.direction in ("BUY_YES", "BUY") else "NO",
                        size_usd=pos.stake_usd,
                        entry_price=pos.entry_price,
                        current_price=pos.current_price,
                        unrealised_pnl=pos.pnl,
                    ))

        finally:
            await client.close()

        self._positions = snapshots
        log.info(
            "engine.positions_checked",
            count=len(snapshots),
            total_pnl=round(sum(s.unrealised_pnl for s in snapshots), 4),
            ws_hits=ws_hits,
            rest_hits=rest_hits,
        )

    async def _maybe_rebalance(self) -> None:
        """Check for portfolio drift and log rebalance signals."""
        interval = self.config.portfolio.rebalance_check_interval_minutes * 60
        now = time.time()
        if now - self._last_rebalance_check < interval:
            return

        self._last_rebalance_check = now
        if not self._positions:
            return

        try:
            signals = self.portfolio.check_rebalance(self._positions)
            if signals:
                for sig in signals:
                    log.warning(
                        "engine.rebalance_signal",
                        type=sig.signal_type,
                        urgency=sig.urgency,
                        description=sig.description,
                    )
                    if self._db:
                        self._db.insert_alert(
                            "warning",
                            f"⚖️ Rebalance: {sig.description}",
                            "risk",
                        )
        except Exception as e:
            log.warning("engine.rebalance_error", error=str(e))

    async def _maybe_scan_arbitrage(self, markets: list[Any]) -> None:
        """Scan for arbitrage opportunities across discovered markets."""
        interval = self.config.portfolio.rebalance_check_interval_minutes * 60
        now = time.time()
        if now - self._last_arbitrage_scan < interval:
            return

        self._last_arbitrage_scan = now
        if not markets:
            return

        try:
            from src.policy.arbitrage import detect_arbitrage
            opps = detect_arbitrage(markets, fee_bps=int(self.config.risk.transaction_fee_pct * 10000))
            self._latest_arb_opportunities = opps
            if opps:
                actionable = [o for o in opps if o.is_actionable]
                log.info(
                    "engine.arbitrage_scan",
                    total=len(opps),
                    actionable=len(actionable),
                )
                for opp in actionable[:3]:
                    if self._db:
                        self._db.insert_alert(
                            "info",
                            f"🔀 Arb: {opp.description}",
                            "arbitrage",
                        )
        except Exception as e:
            log.warning("engine.arbitrage_scan_error", error=str(e))

    async def _maybe_scan_wallets(self) -> None:
        """Run wallet scanner if enabled and interval elapsed."""
        if not self.config.wallet_scanner.enabled:
            return

        interval = self.config.wallet_scanner.scan_interval_minutes * 60
        now = time.time()
        if now - self._last_wallet_scan < interval:
            return

        log.info("engine.wallet_scan_start")
        try:
            result = await self._wallet_scanner.scan()
            self._latest_scan_result = result
            self._last_wallet_scan = now

            # Persist to database
            if self._db:
                import sqlite3
                db_path = self.config.storage.sqlite_path
                conn = sqlite3.connect(db_path)
                try:
                    save_scan_result(conn, result)
                finally:
                    conn.close()

            log.info(
                "engine.wallet_scan_complete",
                wallets=result.wallets_scanned,
                signals=len(result.conviction_signals),
                deltas=len(result.deltas),
            )
        except Exception as e:
            log.warning("engine.wallet_scan_error", error=str(e))

    def get_status(self) -> dict[str, Any]:
        dd_state = self.drawdown.state
        pr_report = self.portfolio.assess(self._positions)
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "live_trading": is_live_trading_enabled(),
            "drawdown": dd_state.to_dict(),
            "portfolio": pr_report.to_dict(),
            "last_cycle": (
                self._cycle_history[-1].to_dict()
                if self._cycle_history else None
            ),
            "positions": len(self._positions),
            "filter_stats": (
                self._last_filter_stats.__dict__
                if self._last_filter_stats else None
            ),
            "research_cache_size": self._research_cache.size(),
        }

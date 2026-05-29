"""CLI entry point for the Polymarket Research & Trading Bot.

Commands:
  bot scan               — List candidate markets
  bot research --market   — Research a specific market
  bot forecast --market   — Produce a forecast for a market
  bot paper-trade --days  — Run paper trading simulation
  bot trade --live        — Live trading (requires ENABLE_LIVE_TRADING=true)
  bot engine start        — Start the continuous trading engine
  bot engine status       — Show engine status
  bot portfolio           — Show portfolio risk report
  bot drawdown            — Show drawdown state
  bot alerts              — Show recent alerts
  bot arbitrage           — Scan for arbitrage opportunities
  bot dashboard           — Launch the monitoring dashboard web UI
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from src.config import BotConfig, load_config, is_live_trading_enabled
from src.observability.logger import configure_logging, get_logger

load_dotenv()

console = Console()
log = get_logger(__name__)


def _run(coro: Any) -> Any:
    """Run an async coroutine from sync CLI."""
    return asyncio.run(coro)


@click.group()
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """Polymarket Research & Trading Bot."""
    ctx.ensure_object(dict)
    cfg = load_config(config_path)
    ctx.obj["config"] = cfg
    configure_logging(
        level=cfg.observability.log_level,
        fmt="console",  # CLI always uses console format
        log_file=cfg.observability.log_file,
    )


# ─── SCAN ────────────────────────────────────────────────────────────

@cli.command()
@click.option("--limit", default=20, help="Number of markets to list")
@click.pass_context
def scan(ctx: click.Context, limit: int) -> None:
    """Scan and list candidate markets."""
    cfg: BotConfig = ctx.obj["config"]

    async def _scan() -> list[dict[str, Any]]:
        from src.connectors.polymarket_gamma import GammaClient

        gamma = GammaClient()
        try:
            markets = await gamma.list_markets(
                limit=min(limit, cfg.scanning.batch_size),
                active=True,
            )
        finally:
            await gamma.close()

        # Filter by scanning config
        candidates = []
        for m in markets:
            if m.volume < cfg.scanning.min_volume_usd:
                continue
            if m.liquidity < cfg.scanning.min_liquidity_usd:
                continue
            if m.spread > cfg.scanning.max_spread:
                continue
            candidates.append(m)

        return candidates

    markets = _run(_scan())

    table = Table(title=f"📊 Candidate Markets ({len(markets)} found)")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Type", style="cyan", max_width=10)
    table.add_column("Question", max_width=50)
    table.add_column("Volume", justify="right", style="green")
    table.add_column("Liquidity", justify="right")
    table.add_column("Implied P", justify="right", style="yellow")
    table.add_column("Spread", justify="right")

    for m in markets:
        table.add_row(
            m.id[:12],
            m.market_type,
            m.question[:50],
            f"${m.volume:,.0f}",
            f"${m.liquidity:,.0f}",
            f"{m.best_bid:.1%}",
            f"{m.spread:.2%}",
        )

    console.print(table)


# ─── RESEARCH ────────────────────────────────────────────────────────

@cli.command()
@click.option("--market", "market_id", required=True, help="Market ID to research")
@click.pass_context
def research(ctx: click.Context, market_id: str) -> None:
    """Research a specific market — fetch evidence and sources."""
    cfg: BotConfig = ctx.obj["config"]

    async def _research() -> dict[str, Any]:
        from src.connectors.polymarket_gamma import GammaClient
        from src.connectors.web_search import create_search_provider
        from src.research.query_builder import build_queries
        from src.research.source_fetcher import SourceFetcher
        from src.research.evidence_extractor import EvidenceExtractor

        gamma = GammaClient()
        search = create_search_provider(cfg.research.search_provider)
        fetcher = SourceFetcher(search, cfg.research)
        extractor = EvidenceExtractor(cfg.forecasting)

        try:
            market = await gamma.get_market(market_id)
            queries = build_queries(market)
            sources = await fetcher.fetch_sources(
                queries, market_type=market.market_type
            )
            evidence = await extractor.extract(
                market.id, market.question, sources,
                market_type=market.market_type,
            )
        finally:
            await gamma.close()
            await search.close()
            await fetcher.close()

        return evidence.to_dict()

    result = _run(_research())

    console.print("\n[bold cyan]📚 Research Results[/bold cyan]\n")
    console.print_json(json.dumps(result, indent=2, default=str))


# ─── FORECAST ────────────────────────────────────────────────────────

@cli.command()
@click.option("--market", "market_id", required=True, help="Market ID to forecast")
@click.pass_context
def forecast(ctx: click.Context, market_id: str) -> None:
    """Produce a full forecast for a market."""
    cfg: BotConfig = ctx.obj["config"]

    async def _forecast() -> dict[str, Any]:
        from src.connectors.polymarket_gamma import GammaClient
        from src.connectors.polymarket_clob import CLOBClient
        from src.connectors.web_search import create_search_provider
        from src.research.query_builder import build_queries
        from src.research.source_fetcher import SourceFetcher
        from src.research.evidence_extractor import EvidenceExtractor
        from src.forecast.feature_builder import build_features
        from src.forecast.llm_forecaster import LLMForecaster
        from src.forecast.calibrator import calibrate
        from src.policy.edge_calc import calculate_edge
        from src.policy.risk_limits import check_risk_limits
        from src.policy.position_sizer import calculate_position_size

        gamma = GammaClient()
        clob = CLOBClient()
        search = create_search_provider(cfg.research.search_provider)
        fetcher = SourceFetcher(search, cfg.research)
        extractor = EvidenceExtractor(cfg.forecasting)
        forecaster = LLMForecaster(cfg.forecasting)

        try:
            # 1. Fetch market data
            market = await gamma.get_market(market_id)
            console.print(f"\n[bold]Market:[/bold] {market.question}")
            console.print(f"[bold]Type:[/bold] {market.market_type}")
            console.print(f"[bold]Implied P:[/bold] {market.best_bid:.1%}")

            # 2. Fetch orderbook
            orderbook = None
            trades = None
            if market.tokens:
                try:
                    token_id = market.tokens[0].token_id
                    orderbook = await clob.get_orderbook(token_id)
                    trades = await clob.get_trade_history(token_id, limit=50)
                except Exception as e:
                    console.print(f"[yellow]⚠ CLOB data unavailable: {e}[/yellow]")

            # 3. Research
            console.print("\n[cyan]🔍 Researching...[/cyan]")
            queries = build_queries(market)
            sources = await fetcher.fetch_sources(
                queries, market_type=market.market_type
            )
            evidence = await extractor.extract(
                market.id, market.question, sources,
                market_type=market.market_type,
            )

            # 4. Build features
            features = build_features(market, orderbook, trades, evidence)

            # 5. LLM forecast
            console.print("[cyan]🧠 Forecasting...[/cyan]")
            raw_forecast = await forecaster.forecast(
                features, evidence,
                resolution_source=market.resolution_source,
            )

            # 6. Calibrate
            cal = calibrate(
                raw_forecast.model_probability,
                evidence.quality_score,
                num_contradictions=len(evidence.contradictions),
                method=cfg.forecasting.calibration_method,
                low_evidence_penalty=cfg.forecasting.low_evidence_penalty,
            )
            model_prob = cal.calibrated_probability

            # 7. Edge calculation
            edge = calculate_edge(features.implied_probability, model_prob)

            # 8. Risk check
            risk_result = check_risk_limits(
                edge=edge,
                features=features,
                risk_config=cfg.risk,
                forecast_config=cfg.forecasting,
                market_type=market.market_type,
                allowed_types=cfg.scanning.preferred_types,
                restricted_types=cfg.scanning.restricted_types,
            )

            # 9. Position sizing (only if TRADE)
            position = None
            if risk_result.allowed:
                position = calculate_position_size(
                    edge, cfg.risk,
                    confidence_level=raw_forecast.confidence_level,
                )

            # Build output
            output: dict[str, Any] = {
                "market_id": market.id,
                "question": market.question,
                "market_type": market.market_type,
                "resolution_source": market.resolution_source,
                "implied_probability": round(features.implied_probability, 4),
                "model_probability": round(model_prob, 4),
                "edge": round(edge.raw_edge, 4),
                "edge_direction": edge.direction,
                "confidence_level": raw_forecast.confidence_level,
                "evidence_quality": round(evidence.quality_score, 2),
                "evidence": [
                    {
                        "text": b.text,
                        "citation": {
                            "url": b.citation.url,
                            "publisher": b.citation.publisher,
                            "date": b.citation.date,
                        },
                    }
                    for b in evidence.bullets[:5]
                ],
                "contradictions": [
                    {"description": c.description}
                    for c in evidence.contradictions
                ],
                "invalidation_triggers": raw_forecast.invalidation_triggers,
                "reasoning": raw_forecast.reasoning,
                "calibration": {
                    "method": cal.method,
                    "adjustments": cal.adjustments,
                },
                "risk_check": {
                    "decision": risk_result.decision,
                    "violations": risk_result.violations,
                    "warnings": risk_result.warnings,
                },
                "decision": risk_result.decision,
            }

            if position:
                output["position"] = {
                    "stake_usd": position.stake_usd,
                    "direction": position.direction,
                    "token_quantity": position.token_quantity,
                    "kelly_fraction": position.kelly_fraction_used,
                    "capped_by": position.capped_by,
                }

            return output

        finally:
            await gamma.close()
            await clob.close()
            await search.close()
            await fetcher.close()

    result = _run(_forecast())

    console.print(f"\n[bold {'green' if result['decision'] == 'TRADE' else 'red'}]"
                  f"Decision: {result['decision']}[/bold {'green' if result['decision'] == 'TRADE' else 'red'}]\n")
    console.print_json(json.dumps(result, indent=2, default=str))


# ─── PAPER TRADE ─────────────────────────────────────────────────────

@cli.command("paper-trade")
@click.option("--days", default=30, help="Number of days to simulate")
@click.option("--markets", default=10, help="Number of markets per cycle")
@click.pass_context
def paper_trade(ctx: click.Context, days: int, markets: int) -> None:
    """Run paper trading simulation."""
    cfg: BotConfig = ctx.obj["config"]
    console.print(f"[bold]📈 Paper Trading Mode[/bold] — {days} days, {markets} markets/cycle")
    console.print("[yellow]⚠ This is a simulation. No real trades will be placed.[/yellow]\n")

    async def _paper_trade() -> None:
        from src.connectors.polymarket_gamma import GammaClient
        from src.storage.database import Database

        db = Database(cfg.storage)
        db.connect()
        gamma = GammaClient()

        try:
            all_markets = await gamma.list_markets(limit=markets, active=True)
            console.print(f"Fetched {len(all_markets)} markets")

            for m in all_markets:
                console.print(f"  [{m.market_type}] {m.question[:60]}  P={m.best_bid:.1%}")

            console.print(f"\n[green]✓ Paper trade scan complete.[/green]")
            console.print(
                "Full paper trading loop (with forecasting) requires API keys.\n"
                "Set OPENAI_API_KEY and SERPAPI_KEY, then use:\n"
                "  bot forecast --market <ID>"
            )
        finally:
            await gamma.close()
            db.close()

    _run(_paper_trade())


# ─── LIVE TRADE ──────────────────────────────────────────────────────

@cli.command()
@click.option("--live", is_flag=True, help="Confirm live trading")
@click.pass_context
def trade(ctx: click.Context, live: bool) -> None:
    """Execute live trades (requires ENABLE_LIVE_TRADING=true)."""
    if not live:
        console.print("[red]❌ Use --live flag to confirm.[/red]")
        sys.exit(1)

    if not is_live_trading_enabled():
        console.print(
            "[red]❌ Live trading is disabled.[/red]\n"
            "Set ENABLE_LIVE_TRADING=true in your environment to enable."
        )
        sys.exit(1)

    cfg: BotConfig = ctx.obj["config"]

    if cfg.risk.kill_switch:
        console.print("[red]❌ Kill switch is ON. Trading halted.[/red]")
        sys.exit(1)

    console.print("[bold red]🚨 LIVE TRADING MODE 🚨[/bold red]")
    console.print("This will place real orders with real money.\n")

    if not click.confirm("Are you sure you want to proceed?"):
        console.print("Aborted.")
        sys.exit(0)

    console.print(
        "[yellow]Live trading loop starting...[/yellow]\n"
        "Implement the full live loop by connecting the forecast pipeline "
        "to the order router. See README for architecture."
    )


# ─── DASHBOARD ───────────────────────────────────────────────────

@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=2345, help="Port to listen on")
@click.option("--debug", is_flag=True, help="Enable Flask debug mode")
@click.option("--no-engine", is_flag=True, help="Don't auto-start the trading engine")
@click.pass_context
def dashboard(ctx: click.Context, host: str, port: int, debug: bool, no_engine: bool) -> None:
    """Launch the monitoring dashboard web UI (with embedded trading engine)."""
    from src.dashboard.app import run_dashboard

    run_dashboard(
        config_path=None,
        host=host,
        port=port,
        debug=debug,
        start_engine=not no_engine,
    )


# ─── ENGINE ──────────────────────────────────────────────────────

@cli.group()
def engine() -> None:
    """Continuous trading engine commands."""
    pass


@engine.command()
@click.pass_context
def start(ctx: click.Context) -> None:
    """Start the continuous trading engine."""
    cfg: BotConfig = ctx.obj["config"]

    console.print("[bold cyan]🤖 Starting Continuous Trading Engine[/bold cyan]")
    console.print(f"  Cycle interval: {cfg.engine.cycle_interval_secs}s")
    console.print(f"  Max markets/cycle: {cfg.engine.max_markets_per_cycle}")
    console.print(f"  Live trading: {is_live_trading_enabled()}")
    console.print(f"  Bankroll: ${cfg.risk.bankroll:,.2f}")
    console.print()

    if cfg.risk.kill_switch:
        console.print("[red]❌ Kill switch is ON. Engine will not trade.[/red]")

    async def _run_engine() -> None:
        from src.engine.loop import TradingEngine

        eng = TradingEngine(config=cfg)
        try:
            await eng.start()
        except KeyboardInterrupt:
            eng.stop()
            console.print("\n[yellow]Engine stopped by user.[/yellow]")

    _run(_run_engine())


@engine.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show engine status summary."""
    console.print("[bold]📊 Engine Status[/bold]")
    console.print("Engine status is available on the dashboard at localhost:2345")


# ─── PORTFOLIO ───────────────────────────────────────────────────

@cli.command()
@click.pass_context
def portfolio(ctx: click.Context) -> None:
    """Show portfolio risk report."""
    cfg: BotConfig = ctx.obj["config"]

    from src.policy.portfolio_risk import PortfolioRiskManager
    from src.storage.database import Database

    db = Database(cfg.storage)
    db.connect()

    manager = PortfolioRiskManager(cfg.risk.bankroll, cfg)
    # In a real scenario, positions would be loaded from DB
    report = manager.assess([])

    table = Table(title="📊 Portfolio Risk Report")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Total Exposure", f"${report.total_exposure_usd:,.2f}")
    table.add_row("Open Positions", str(report.num_positions))
    table.add_row("Unrealised P&L", f"${report.total_unrealised_pnl:,.2f}")
    table.add_row("Largest Position", f"{report.largest_position_pct:.1%}")
    table.add_row("Portfolio Healthy", "✅" if report.is_healthy else "❌")

    if report.category_violations:
        for v in report.category_violations:
            table.add_row("[red]Category Violation[/red]", v)
    if report.event_violations:
        for v in report.event_violations:
            table.add_row("[red]Event Violation[/red]", v)

    console.print(table)
    db.close()


# ─── DRAWDOWN ────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def drawdown(ctx: click.Context) -> None:
    """Show current drawdown state."""
    cfg: BotConfig = ctx.obj["config"]

    from src.policy.drawdown import DrawdownManager

    manager = DrawdownManager(cfg.risk.bankroll, cfg)
    state = manager.state

    table = Table(title="📉 Drawdown State")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Peak Equity", f"${state.peak_equity:,.2f}")
    table.add_row("Current Equity", f"${state.current_equity:,.2f}")
    table.add_row("Drawdown", f"{state.drawdown_pct:.1%}")
    table.add_row("Drawdown USD", f"${state.drawdown_usd:,.2f}")
    table.add_row("Heat Level", str(state.heat_level))
    table.add_row("Kelly Multiplier", f"{state.kelly_multiplier:.2f}")
    table.add_row(
        "Kill Switch",
        "[red]ENGAGED[/red]" if state.is_killed else "[green]OFF[/green]",
    )

    console.print(table)


# ─── ARBITRAGE ───────────────────────────────────────────────────

@cli.command()
@click.option("--limit", default=50, help="Number of markets to scan")
@click.pass_context
def arbitrage(ctx: click.Context, limit: int) -> None:
    """Scan for arbitrage opportunities across markets."""
    cfg: BotConfig = ctx.obj["config"]

    async def _scan() -> list[dict]:
        from src.connectors.polymarket_gamma import GammaClient
        from src.policy.arbitrage import detect_arbitrage

        gamma = GammaClient()
        try:
            markets = await gamma.list_markets(limit=limit, active=True)
            opportunities = detect_arbitrage(markets)
            return [o.to_dict() for o in opportunities]
        finally:
            await gamma.close()

    opps = _run(_scan())

    if not opps:
        console.print("[yellow]No arbitrage opportunities found.[/yellow]")
        return

    table = Table(title=f"🔀 Arbitrage Opportunities ({len(opps)} found)")
    table.add_column("Type", style="cyan")
    table.add_column("Edge", justify="right", style="green")
    table.add_column("Actionable", justify="center")
    table.add_column("Description", max_width=60)

    for o in opps[:20]:
        table.add_row(
            o["arb_type"],
            f"{o['arb_edge']:.3f}",
            "✅" if o["is_actionable"] else "❌",
            o["description"][:60],
        )

    console.print(table)


# ─── ALERTS ──────────────────────────────────────────────────────

@cli.command()
@click.option("--limit", default=20, help="Number of alerts to show")
@click.pass_context
def alerts(ctx: click.Context, limit: int) -> None:
    """Show recent alerts."""
    console.print("[bold]🔔 Recent Alerts[/bold]")
    console.print("Alert history is available on the dashboard at localhost:2345")
    console.print("Configure alert channels in config.yaml under 'alerts'")


if __name__ == "__main__":
    cli()

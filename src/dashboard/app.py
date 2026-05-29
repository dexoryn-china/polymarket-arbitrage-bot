"""Dashboard — Flask web application for monitoring the bot.

Serves a single-page dashboard at http://localhost:2345 with:
  - Portfolio overview (bankroll, P&L, win rate)
  - Active positions table
  - Recent forecasts with evidence quality
  - Trade history
  - Risk monitor (limits vs current values)
  - System health & metrics
  - Embedded trading engine with start/stop controls

All data is read from the SQLite database and in-process metrics.
The trading engine runs in a background thread alongside the dashboard.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env from project root (explicit path for reliability)
_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env", override=True)

from flask import Flask, jsonify, render_template, request, send_from_directory

import yaml

from src.config import BotConfig, load_config, is_live_trading_enabled
from src.observability.metrics import metrics
from src.observability.sentry_integration import init_sentry

# Initialise Sentry if SENTRY_DSN is set
init_sentry()

# ─── Flask app ──────────────────────────────────────────────────────

_here = Path(__file__).resolve().parent
_PROJECT_ROOT = _here.parent.parent
app = Flask(
    __name__,
    template_folder=str(_here / "templates"),
    static_folder=str(_here / "static"),
)

_config: BotConfig | None = None
_db_path: str = "data/bot.db"

# ─── Embedded Engine ────────────────────────────────────────────────

_engine_thread: threading.Thread | None = None
_engine_instance: Any = None
_engine_loop: asyncio.AbstractEventLoop | None = None
_engine_started_at: float = 0.0
_engine_error: str | None = None


def _engine_worker(cfg: BotConfig) -> None:
    """Run the TradingEngine in a dedicated thread with its own event loop."""
    global _engine_instance, _engine_loop, _engine_started_at, _engine_error
    from src.engine.loop import TradingEngine

    _engine_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_engine_loop)

    _engine_instance = TradingEngine(config=cfg)
    _engine_started_at = time.time()
    _engine_error = None

    try:
        _engine_loop.run_until_complete(_engine_instance.start())
    except Exception as e:
        _engine_error = str(e)
        import traceback
        traceback.print_exc()
    finally:
        _engine_loop.close()
        _engine_loop = None


def _start_engine(cfg: BotConfig) -> bool:
    """Start the engine in a background thread.  Returns True on success."""
    global _engine_thread, _engine_error
    if _engine_thread and _engine_thread.is_alive():
        return False  # already running
    _engine_error = None
    _engine_thread = threading.Thread(
        target=_engine_worker, args=(cfg,), daemon=True, name="trading-engine",
    )
    _engine_thread.start()
    return True


def _stop_engine() -> bool:
    """Ask the engine to stop gracefully."""
    global _engine_instance
    if _engine_instance and _engine_instance.is_running:
        _engine_instance.stop()
        return True
    return False


# ─── Maintenance Worker (backup, VACUUM, equity snapshots) ─────────

_maintenance_thread: threading.Thread | None = None

def _maintenance_worker() -> None:
    """Background thread: periodic backup, VACUUM, equity snapshots."""
    import shutil

    BACKUP_INTERVAL = 6 * 3600   # 6 hours
    VACUUM_INTERVAL = 24 * 3600  # daily
    EQUITY_INTERVAL = 300        # 5 minutes

    last_backup = time.time()
    last_vacuum = time.time()
    last_equity = 0.0

    while True:
        try:
            now = time.time()

            # ── Equity snapshot ──
            if now - last_equity >= EQUITY_INTERVAL:
                last_equity = now
                try:
                    conn = _get_conn()
                    _ensure_tables(conn)
                    cfg = _get_config()
                    bankroll = cfg.risk.bankroll

                    positions = conn.execute(
                        "SELECT pnl, stake_usd FROM positions"
                    ).fetchall()
                    closed = conn.execute(
                        "SELECT pnl FROM closed_positions"
                    ).fetchall()

                    invested = sum(float(r["stake_usd"] or 0) for r in positions)
                    unrealised = sum(float(r["pnl"] or 0) for r in positions)
                    realised = sum(float(r["pnl"] or 0) for r in closed)
                    equity = bankroll + unrealised + realised
                    cash = bankroll - invested + realised
                    peak = max(bankroll, equity)
                    dd = ((peak - equity) / peak) if peak > 0 else 0.0

                    conn.execute(
                        """INSERT INTO equity_snapshots
                            (timestamp, equity, invested, cash, unrealised_pnl,
                             realised_pnl, num_positions, daily_var, drawdown_pct)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        (dt.datetime.now(dt.timezone.utc).isoformat(),
                         round(equity, 2), round(invested, 2), round(cash, 2),
                         round(unrealised, 2), round(realised, 2),
                         len(positions), 0.0, round(dd, 4)),
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

            # ── Backup ──
            if now - last_backup >= BACKUP_INTERVAL:
                last_backup = now
                try:
                    db_file = Path(_db_path)
                    if db_file.exists():
                        backup_dir = db_file.parent / "backups"
                        backup_dir.mkdir(exist_ok=True)
                        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                        dst = backup_dir / f"bot_{ts}.db"
                        shutil.copy2(str(db_file), str(dst))
                        # Prune old backups (keep last 10)
                        backups = sorted(backup_dir.glob("bot_*.db"))
                        for old in backups[:-10]:
                            old.unlink()
                except Exception:
                    pass

            # ── VACUUM ──
            if now - last_vacuum >= VACUUM_INTERVAL:
                last_vacuum = now
                try:
                    conn = _get_conn()
                    conn.execute("VACUUM")
                    conn.close()
                except Exception:
                    pass

            time.sleep(60)
        except Exception:
            time.sleep(60)


def _start_maintenance() -> None:
    global _maintenance_thread
    if _maintenance_thread and _maintenance_thread.is_alive():
        return
    _maintenance_thread = threading.Thread(
        target=_maintenance_worker, daemon=True, name="maintenance",
    )
    _maintenance_thread.start()


def _get_config() -> BotConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist (for fresh dashboards)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS markets (
            id TEXT PRIMARY KEY, condition_id TEXT, question TEXT,
            market_type TEXT, category TEXT, volume REAL DEFAULT 0,
            liquidity REAL DEFAULT 0, end_date TEXT, resolution_source TEXT,
            first_seen TEXT, last_updated TEXT
        );
        CREATE TABLE IF NOT EXISTS forecasts (
            id TEXT PRIMARY KEY, market_id TEXT NOT NULL, question TEXT,
            market_type TEXT, implied_probability REAL, model_probability REAL,
            edge REAL, confidence_level TEXT, evidence_quality REAL,
            num_sources INTEGER, decision TEXT, reasoning TEXT,
            evidence_json TEXT, invalidation_triggers_json TEXT,
            research_evidence_json TEXT DEFAULT '{}', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY, order_id TEXT, market_id TEXT NOT NULL,
            token_id TEXT, side TEXT, price REAL, size REAL, stake_usd REAL,
            status TEXT, dry_run INTEGER DEFAULT 1, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS positions (
            market_id TEXT PRIMARY KEY, token_id TEXT, direction TEXT,
            entry_price REAL, size REAL, stake_usd REAL,
            current_price REAL, pnl REAL, opened_at TEXT,
            question TEXT, market_type TEXT
        );
        CREATE TABLE IF NOT EXISTS engine_state (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER, market_id TEXT, question TEXT, market_type TEXT,
            implied_prob REAL, model_prob REAL, edge REAL,
            evidence_quality REAL, num_sources INTEGER, confidence TEXT,
            decision TEXT, decision_reasons TEXT,
            stake_usd REAL DEFAULT 0, order_status TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT DEFAULT 'info', channel TEXT DEFAULT 'system',
            message TEXT, market_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS watchlist (
            market_id TEXT PRIMARY KEY, question TEXT,
            category TEXT DEFAULT '', notes TEXT DEFAULT '',
            added_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL, question TEXT, direction TEXT,
            entry_price REAL DEFAULT 0, exit_price REAL DEFAULT 0,
            stake_usd REAL DEFAULT 0, pnl REAL DEFAULT 0,
            annotation TEXT DEFAULT '', reasoning TEXT DEFAULT '',
            lessons_learned TEXT DEFAULT '', tags TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, equity REAL NOT NULL,
            invested REAL DEFAULT 0, cash REAL DEFAULT 0,
            unrealised_pnl REAL DEFAULT 0, realised_pnl REAL DEFAULT 0,
            num_positions INTEGER DEFAULT 0, daily_var REAL DEFAULT 0,
            drawdown_pct REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS var_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, daily_var_95 REAL DEFAULT 0,
            daily_var_99 REAL DEFAULT 0, portfolio_value REAL DEFAULT 0,
            num_positions INTEGER DEFAULT 0, method TEXT DEFAULT 'parametric',
            details_json TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS closed_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL, question TEXT, direction TEXT,
            entry_price REAL, exit_price REAL, stake_usd REAL, pnl REAL,
            close_reason TEXT DEFAULT '', closed_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS whale_stars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            star_type TEXT NOT NULL,
            identifier TEXT NOT NULL,
            label TEXT DEFAULT '',
            starred_at TEXT DEFAULT (datetime('now')),
            UNIQUE(star_type, identifier)
        );
        CREATE TABLE IF NOT EXISTS mentor_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            whale_address TEXT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS whale_alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            detail_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(alert_type, message)
        );
        CREATE TABLE IF NOT EXISTS conviction_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_slug TEXT NOT NULL,
            outcome TEXT DEFAULT '',
            conviction_score REAL DEFAULT 0,
            whale_count INTEGER DEFAULT 0,
            total_whale_usd REAL DEFAULT 0,
            snapped_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS whale_scan_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER DEFAULT 0,
            interval_minutes INTEGER DEFAULT 15,
            min_volume REAL DEFAULT 50000,
            min_liquidity REAL DEFAULT 10000,
            min_win_rate REAL DEFAULT 0.45,
            min_pnl REAL DEFAULT 5000,
            min_positions INTEGER DEFAULT 5,
            max_candidates INTEGER DEFAULT 50,
            last_scan_at TEXT,
            last_scan_status TEXT DEFAULT 'idle',
            last_scan_markets INTEGER DEFAULT 0,
            last_scan_wallets INTEGER DEFAULT 0,
            last_scan_candidates INTEGER DEFAULT 0,
            last_scan_duration_s REAL DEFAULT 0,
            last_scan_error TEXT DEFAULT '',
            last_scan_trades_analyzed INTEGER DEFAULT 0,
            last_scan_addresses_discovered INTEGER DEFAULT 0,
            last_scan_markets_json TEXT DEFAULT '[]',
            total_scans INTEGER DEFAULT 0
        );
        INSERT OR IGNORE INTO whale_scan_config (id) VALUES (1);
        CREATE TABLE IF NOT EXISTS whale_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL UNIQUE,
            name TEXT DEFAULT '',
            total_pnl REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            active_positions INTEGER DEFAULT 0,
            total_volume REAL DEFAULT 0,
            avg_position_size REAL DEFAULT 0,
            liquid_market_count INTEGER DEFAULT 0,
            liquid_market_pct REAL DEFAULT 0,
            score REAL DEFAULT 0,
            grade TEXT DEFAULT 'C',
            status TEXT DEFAULT 'candidate',
            source TEXT DEFAULT 'scan',
            top_markets_json TEXT DEFAULT '[]',
            scan_data_json TEXT DEFAULT '{}',
            discovered_at TEXT DEFAULT (datetime('now')),
            last_scanned_at TEXT DEFAULT (datetime('now')),
            promoted_at TEXT
        );
    """)
    # Migrate: add new scanner columns if missing
    for col, defn in [
        ("last_scan_trades_analyzed", "INTEGER DEFAULT 0"),
        ("last_scan_addresses_discovered", "INTEGER DEFAULT 0"),
        ("last_scan_markets_json", "TEXT DEFAULT '[]'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE whale_scan_config ADD COLUMN {col} {defn}")
        except Exception:
            pass  # column already exists


# ─── Dashboard Authentication ──────────────────────────────────────

_DASHBOARD_API_KEY = os.environ.get("DASHBOARD_API_KEY", "")


def _check_auth() -> bool:
    """Check if request is authenticated. Returns True if auth passes."""
    if not _DASHBOARD_API_KEY:
        return True  # No auth configured — open access
    # Accept via header or query param
    token = request.headers.get("X-API-Key") or request.args.get("api_key", "")
    return token == _DASHBOARD_API_KEY


@app.before_request
def _require_auth():
    """Enforce auth on all routes except health checks and static assets."""
    # Health and readiness probes are always open
    if request.path in ("/health", "/ready", "/metrics"):
        return None
    # Static assets (CSS, JS, images) must be accessible without auth
    if request.path.startswith("/static/"):
        return None
    if not _check_auth():
        return jsonify({"error": "unauthorized", "message": "Set X-API-Key header or ?api_key= param"}), 401
    return None


# ─── Health & Readiness ────────────────────────────────────────────

@app.route("/health")
def health() -> Any:
    """Liveness probe — returns 200 if the Flask process is up."""
    return jsonify({"status": "ok", "service": "polymarket-bot"})


@app.route("/ready")
def ready() -> Any:
    """Readiness probe — checks DB connectivity and engine state."""
    checks: dict[str, Any] = {"db": False, "engine": False}
    try:
        conn = _get_conn()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        checks["db"] = True
    except Exception as e:
        checks["db_error"] = str(e)

    checks["engine"] = bool(_engine_instance and _engine_instance.is_running)
    all_ok = checks["db"]  # Engine not required to be ready
    status_code = 200 if all_ok else 503
    return jsonify({"status": "ready" if all_ok else "not_ready", "checks": checks}), status_code


# ─── Prometheus Metrics ─────────────────────────────────────────────

@app.route("/metrics")
def prometheus_metrics() -> Any:
    """Expose metrics in Prometheus text exposition format."""
    from src.connectors.rate_limiter import rate_limiter as _rl

    snap = metrics.snapshot()
    lines: list[str] = []

    # Counters
    for name, value in snap.get("counters", {}).items():
        safe = name.replace(".", "_").replace("-", "_")
        lines.append(f"# TYPE bot_{safe} counter")
        lines.append(f"bot_{safe} {value}")

    # Gauges
    for name, value in snap.get("gauges", {}).items():
        safe = name.replace(".", "_").replace("-", "_")
        lines.append(f"# TYPE bot_{safe} gauge")
        lines.append(f"bot_{safe} {value}")

    # Rate limiter stats
    for endpoint, stats in _rl.stats().items():
        safe = endpoint.replace(".", "_").replace("-", "_")
        lines.append(f'bot_rate_limiter_requests_total{{endpoint="{safe}"}} {stats["total_requests"]}')
        lines.append(f'bot_rate_limiter_waits_total{{endpoint="{safe}"}} {stats["total_waits"]}')

    # Engine cycle gauge
    if _engine_instance:
        lines.append(f"bot_engine_cycles_total {_engine_instance._cycle_count}")
        lines.append(f"bot_engine_running {1 if _engine_instance.is_running else 0}")

    body = "\n".join(lines) + "\n"
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ─── Pages ──────────────────────────────────────────────────────────

@app.route("/")
def index() -> str:
    return render_template("index.html")


# ─── API: Portfolio Overview ────────────────────────────────────────

@app.route("/api/portfolio")
def api_portfolio() -> Any:
    cfg = _get_config()
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Bankroll from config
        bankroll = cfg.risk.bankroll

        # Open positions
        positions = conn.execute("SELECT * FROM positions").fetchall()
        open_count = len(positions)
        total_invested = sum(r["stake_usd"] or 0 for r in positions)
        unrealized_pnl = sum(r["pnl"] or 0 for r in positions)

        # Trades today
        today = dt.date.today().isoformat()
        today_trades = conn.execute(
            "SELECT * FROM trades WHERE date(created_at) = ?", (today,)
        ).fetchall()
        daily_volume = sum(r["stake_usd"] or 0 for r in today_trades)

        # All-time trades
        all_trades = conn.execute("SELECT * FROM trades").fetchall()
        total_trades = len(all_trades)
        live_trades = [t for t in all_trades if not t["dry_run"]]
        paper_trades = [t for t in all_trades if t["dry_run"]]

        # Realized P&L from closed positions
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS realized FROM closed_positions"
            ).fetchone()
            realized_pnl = float(row["realized"]) if row else 0.0
        except Exception:
            realized_pnl = 0.0

        # Total P&L = realized (closed) + unrealized (open)
        total_pnl = realized_pnl + unrealized_pnl

        # Best / worst position (across open AND closed)
        open_pnls = [r["pnl"] or 0 for r in positions]
        try:
            closed_best = conn.execute(
                "SELECT MAX(pnl) AS v FROM closed_positions"
            ).fetchone()
            closed_worst = conn.execute(
                "SELECT MIN(pnl) AS v FROM closed_positions"
            ).fetchone()
            cb = float(closed_best["v"]) if closed_best and closed_best["v"] is not None else 0.0
            cw = float(closed_worst["v"]) if closed_worst and closed_worst["v"] is not None else 0.0
        except Exception:
            cb, cw = 0.0, 0.0
        best_pnl = max(max(open_pnls, default=0.0), cb)
        worst_pnl = min(min(open_pnls, default=0.0), cw)

        # Win rate from forecasts
        forecasts = conn.execute("SELECT * FROM forecasts").fetchall()
        trade_decisions = [f for f in forecasts if f["decision"] == "TRADE"]
        no_trade_decisions = [f for f in forecasts if f["decision"] == "NO TRADE"]

        # Average evidence quality
        eq_values = [f["evidence_quality"] for f in forecasts if f["evidence_quality"]]
        avg_evidence_quality = sum(eq_values) / len(eq_values) if eq_values else 0.0

        # Average edge
        edge_values = [f["edge"] for f in forecasts if f["edge"]]
        avg_edge = sum(edge_values) / len(edge_values) if edge_values else 0.0

        return jsonify({
            "bankroll": bankroll,
            "available_capital": bankroll - total_invested,
            "total_invested": total_invested,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl,
            "total_pnl": total_pnl,
            "best_pnl": round(best_pnl, 4),
            "worst_pnl": round(worst_pnl, 4),
            "open_positions": open_count,
            "total_trades": total_trades,
            "live_trades": len(live_trades),
            "paper_trades": len(paper_trades),
            "daily_volume": daily_volume,
            "today_trades": len(today_trades),
            "total_forecasts": len(forecasts),
            "trade_decisions": len(trade_decisions),
            "no_trade_decisions": len(no_trade_decisions),
            "avg_evidence_quality": round(avg_evidence_quality, 3),
            "avg_edge": round(avg_edge, 4),
            "dry_run": cfg.execution.dry_run,
            "live_trading_enabled": is_live_trading_enabled(),
        })
    finally:
        conn.close()


# ─── API: Active Positions ─────────────────────────────────────────

@app.route("/api/positions")
def api_positions() -> Any:
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute("""
            SELECT p.*, m.question, m.market_type
            FROM positions p
            LEFT JOIN markets m ON p.market_id = m.id
            ORDER BY p.opened_at DESC
        """).fetchall()
        positions = []
        total_pnl = 0.0
        total_invested = 0.0
        for r in rows:
            rd = dict(r)
            pnl = rd.get("pnl") or 0.0
            entry = rd.get("entry_price") or 0.0
            current = rd.get("current_price") or 0.0
            stake = rd.get("stake_usd") or 0.0

            # PNL percentage based on stake invested
            pnl_pct = (pnl / stake * 100) if stake > 0 else 0.0
            rd["pnl_pct"] = round(pnl_pct, 2)

            # Price change from entry
            price_change = current - entry
            price_change_pct = (price_change / entry * 100) if entry > 0 else 0.0
            rd["price_change"] = round(price_change, 4)
            rd["price_change_pct"] = round(price_change_pct, 2)

            # Time held
            if rd.get("opened_at"):
                try:
                    opened = dt.datetime.fromisoformat(rd["opened_at"].replace("Z", "+00:00"))
                    held = dt.datetime.now(dt.timezone.utc) - opened
                    rd["hours_held"] = round(held.total_seconds() / 3600, 1)
                except (ValueError, TypeError):
                    rd["hours_held"] = 0
            else:
                rd["hours_held"] = 0

            total_pnl += pnl
            total_invested += stake
            positions.append(rd)

        return jsonify({
            "positions": positions,
            "summary": {
                "count": len(positions),
                "total_pnl": round(total_pnl, 4),
                "total_invested": round(total_invested, 2),
                "pnl_pct": round(total_pnl / total_invested * 100, 2) if total_invested > 0 else 0.0,
                "winners": sum(1 for p in positions if (p.get("pnl") or 0) > 0),
                "losers": sum(1 for p in positions if (p.get("pnl") or 0) < 0),
                "flat": sum(1 for p in positions if (p.get("pnl") or 0) == 0),
            },
        })
    finally:
        conn.close()


# ─── API: Position Detail ──────────────────────────────────────────

@app.route("/api/positions/<market_id>")
def api_position_detail(market_id: str) -> Any:
    """Return comprehensive detail for a single active position."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        cfg = _get_config()
        sl_pct = cfg.risk.stop_loss_pct
        tp_pct = cfg.risk.take_profit_pct
        max_hold_hours = getattr(cfg.risk, "max_holding_hours", 72.0)

        # ── Position + market data ───────────────────────────────
        row = conn.execute("""
            SELECT p.*, m.question AS mkt_question, m.market_type AS mkt_type,
                   m.category, m.volume, m.liquidity, m.end_date,
                   m.resolution_source
            FROM positions p
            LEFT JOIN markets m ON p.market_id = m.id
            WHERE p.market_id = ?
        """, (market_id,)).fetchone()

        if not row:
            return jsonify({"error": "Position not found"}), 404

        pos = dict(row)
        entry = float(pos.get("entry_price") or 0)
        current = float(pos.get("current_price") or 0)
        stake = float(pos.get("stake_usd") or 0)
        pnl = float(pos.get("pnl") or 0)
        size = float(pos.get("size") or 0)
        direction = pos.get("direction") or ""

        # Prefer market table question, fall back to position question
        question = pos.get("mkt_question") or pos.get("question") or pos.get("market_id", "")
        market_type = pos.get("mkt_type") or pos.get("market_type") or "—"

        # ── P&L calculations ─────────────────────────────────────
        pnl_pct = (pnl / stake * 100) if stake > 0 else 0.0

        # ── Holding duration ─────────────────────────────────────
        holding_hours = 0.0
        holding_label = "—"
        opened_at = pos.get("opened_at", "")
        if opened_at:
            try:
                opened = dt.datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                delta = dt.datetime.now(dt.timezone.utc) - opened
                holding_hours = delta.total_seconds() / 3600
                if holding_hours >= 24:
                    days = holding_hours / 24
                    holding_label = f"{days:.1f} days ({holding_hours:.0f}h)"
                else:
                    holding_label = f"{holding_hours:.1f} hours"
            except (ValueError, TypeError):
                pass

        # ── TP/SL proximity ──────────────────────────────────────
        # How close (as %) to hitting stop loss or take profit
        sl_trigger_pnl_pct = -sl_pct * 100  # e.g. -20%
        tp_trigger_pnl_pct = tp_pct * 100   # e.g. +30%

        # Distance to SL/TP as fraction of the full range
        # SL: how far along from 0% → -sl_pct we are (0=fresh, 1=hit)
        sl_proximity = 0.0
        if sl_pct > 0 and pnl_pct < 0:
            sl_proximity = min(abs(pnl_pct) / (sl_pct * 100), 1.0)
        # TP: how far along from 0% → +tp_pct we are (0=fresh, 1=hit)
        tp_proximity = 0.0
        if tp_pct > 0 and pnl_pct > 0:
            tp_proximity = min(pnl_pct / (tp_pct * 100), 1.0)

        # SL/TP in price terms
        if direction in ("BUY_YES", "BUY"):
            sl_price = entry * (1 - sl_pct) if sl_pct > 0 else None
            tp_price = entry * (1 + tp_pct) if tp_pct > 0 else None
        else:
            sl_price = entry * (1 + sl_pct) if sl_pct > 0 else None
            tp_price = entry * (1 - tp_pct) if tp_pct > 0 else None

        # ── Max holding period ───────────────────────────────────
        holding_pct = (holding_hours / max_hold_hours * 100) if max_hold_hours > 0 else 0.0
        time_remaining_hours = max(max_hold_hours - holding_hours, 0) if max_hold_hours > 0 else None

        # ── Latest forecast for this market ──────────────────────
        fc_row = conn.execute("""
            SELECT model_probability, implied_probability, edge,
                   confidence_level, evidence_quality, num_sources,
                   decision, reasoning, created_at
            FROM forecasts
            WHERE market_id = ?
            ORDER BY created_at DESC LIMIT 1
        """, (market_id,)).fetchone()
        forecast = dict(fc_row) if fc_row else None

        # ── Entry trade info ─────────────────────────────────────
        trade_row = conn.execute("""
            SELECT id, order_id, side, price, size, stake_usd, status,
                   dry_run, created_at
            FROM trades
            WHERE market_id = ? AND side != 'SELL'
            ORDER BY created_at DESC LIMIT 1
        """, (market_id,)).fetchone()
        entry_trade = dict(trade_row) if trade_row else None

        # ── Build response ───────────────────────────────────────
        result = {
            "market_id": market_id,
            "question": question,
            "market_type": market_type,
            "category": pos.get("category") or "—",
            "direction": direction,
            "token_id": pos.get("token_id") or "",

            # Price & P&L
            "entry_price": round(entry, 4),
            "current_price": round(current, 4),
            "price_change": round(current - entry, 4),
            "price_change_pct": round(((current - entry) / entry * 100) if entry > 0 else 0, 2),
            "size": round(size, 2),
            "stake_usd": round(stake, 2),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),

            # Market info
            "volume": round(float(pos.get("volume") or 0), 2),
            "liquidity": round(float(pos.get("liquidity") or 0), 2),
            "end_date": pos.get("end_date") or "—",
            "resolution_source": pos.get("resolution_source") or "—",

            # Time info
            "opened_at": opened_at,
            "holding_hours": round(holding_hours, 1),
            "holding_label": holding_label,
            "max_holding_hours": max_hold_hours,
            "holding_pct": round(holding_pct, 1),
            "time_remaining_hours": round(time_remaining_hours, 1) if time_remaining_hours is not None else None,

            # TP/SL
            "stop_loss_pct": sl_pct,
            "take_profit_pct": tp_pct,
            "sl_trigger_pnl_pct": round(sl_trigger_pnl_pct, 1),
            "tp_trigger_pnl_pct": round(tp_trigger_pnl_pct, 1),
            "sl_proximity": round(sl_proximity, 3),
            "tp_proximity": round(tp_proximity, 3),
            "sl_price": round(sl_price, 4) if sl_price is not None else None,
            "tp_price": round(tp_price, 4) if tp_price is not None else None,

            # Forecast
            "forecast": forecast,

            # Entry trade
            "entry_trade": entry_trade,

            # Polymarket link
            "polymarket_url": f"https://polymarket.com/event/{market_id}",
        }

        return jsonify(result)

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─── API: Recent Forecasts ─────────────────────────────────────────

@app.route("/api/forecasts")
def api_forecasts() -> Any:
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute("""
            SELECT * FROM forecasts ORDER BY created_at DESC LIMIT 50
        """).fetchall()
        forecasts = []
        for r in rows:
            rd = dict(r)
            # Parse evidence JSON for bullet count
            try:
                evidence = json.loads(rd.get("evidence_json", "[]"))
                rd["evidence_count"] = len(evidence) if isinstance(evidence, list) else 0
            except (json.JSONDecodeError, TypeError):
                rd["evidence_count"] = 0
            forecasts.append(rd)
        return jsonify({"forecasts": forecasts})
    finally:
        conn.close()


# ─── API: Trade History (Enhanced) ─────────────────────────────────

@app.route("/api/trades")
def api_trades() -> Any:
    """Comprehensive trade history merging open positions, closed positions,
    and trade records.  Each row shows status, close reason (TP/SL hit etc.),
    accurate PnL, duration, and whether the trade is still active."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        cfg = _get_config()
        sl_pct = getattr(cfg.risk, "stop_loss_pct", 0.20)
        tp_pct = getattr(cfg.risk, "take_profit_pct", 0.30)

        unified: list[dict] = []

        # ── 1) Active positions (still open) ─────────────────────
        open_rows = conn.execute("""
            SELECT p.market_id, p.token_id, p.direction, p.entry_price,
                   p.current_price, p.size, p.stake_usd, p.pnl, p.opened_at,
                   m.question, m.market_type
            FROM positions p
            LEFT JOIN markets m ON p.market_id = m.id
            ORDER BY p.opened_at DESC
        """).fetchall()
        now = dt.datetime.now(dt.timezone.utc)
        for r in open_rows:
            d = dict(r)
            entry = float(d.get("entry_price") or 0)
            current = float(d.get("current_price") or entry)
            stake = float(d.get("stake_usd") or 0)
            pnl = float(d.get("pnl") or 0)
            pnl_pct = (pnl / stake * 100) if stake > 0 else 0.0

            # Holding duration
            hours_held = 0.0
            opened_at = d.get("opened_at", "")
            if opened_at:
                try:
                    opened = dt.datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                    hours_held = (now - opened).total_seconds() / 3600
                except (ValueError, TypeError):
                    pass

            # TP/SL proximity
            sl_dist = min(abs(pnl_pct) / (sl_pct * 100), 1.0) if sl_pct > 0 and pnl_pct < 0 else 0.0
            tp_dist = min(pnl_pct / (tp_pct * 100), 1.0) if tp_pct > 0 and pnl_pct > 0 else 0.0

            unified.append({
                "market_id": d["market_id"],
                "question": d.get("question") or d["market_id"],
                "market_type": d.get("market_type") or "—",
                "direction": d.get("direction") or "—",
                "entry_price": entry,
                "exit_price": None,
                "current_price": current,
                "size": float(d.get("size") or 0),
                "stake_usd": stake,
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 2),
                "trade_status": "ACTIVE",
                "close_reason": None,
                "close_reason_label": None,
                "opened_at": opened_at,
                "closed_at": None,
                "hours_held": round(hours_held, 1),
                "is_paper": True,
                "sl_proximity": round(sl_dist, 3),
                "tp_proximity": round(tp_dist, 3),
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
            })

        # ── 2) Closed positions (archived) ───────────────────────
        # Check table exists
        cp_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='closed_positions'"
        ).fetchone()
        if cp_exists:
            closed_rows = conn.execute("""
                SELECT cp.*, m.question AS mkt_question, m.market_type AS mkt_type
                FROM closed_positions cp
                LEFT JOIN markets m ON cp.market_id = m.id
                ORDER BY cp.closed_at DESC
                LIMIT 200
            """).fetchall()
            for r in closed_rows:
                d = dict(r)
                entry = float(d.get("entry_price") or 0)
                exit_p = float(d.get("exit_price") or 0)
                stake = float(d.get("stake_usd") or 0)
                pnl = float(d.get("pnl") or 0)
                pnl_pct = (pnl / stake * 100) if stake > 0 else 0.0

                # Duration
                hours_held = 0.0
                opened_at = d.get("opened_at", "")
                closed_at = d.get("closed_at", "")
                if opened_at and closed_at:
                    try:
                        op = dt.datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                        cl = dt.datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                        hours_held = (cl - op).total_seconds() / 3600
                    except (ValueError, TypeError):
                        pass

                # Close reason mapping
                raw_reason = (d.get("close_reason") or "").upper()
                reason_labels = {
                    "STOP_LOSS": "🔴 Stop Loss Hit",
                    "TAKE_PROFIT": "🟢 Take Profit Hit",
                    "MARKET_RESOLVED": "📋 Market Resolved",
                    "MAX_HOLDING": "⏰ Max Hold Time",
                    "MANUAL": "👤 Manual Close",
                }
                reason_label = reason_labels.get(raw_reason, raw_reason or "Closed")

                # Determine status
                if raw_reason == "TAKE_PROFIT":
                    trade_status = "TP_HIT"
                elif raw_reason == "STOP_LOSS":
                    trade_status = "SL_HIT"
                elif raw_reason == "MARKET_RESOLVED":
                    trade_status = "RESOLVED"
                elif raw_reason == "MAX_HOLDING":
                    trade_status = "TIME_EXIT"
                else:
                    trade_status = "CLOSED"

                unified.append({
                    "market_id": d["market_id"],
                    "question": d.get("mkt_question") or d.get("question") or d["market_id"],
                    "market_type": d.get("mkt_type") or d.get("market_type") or "—",
                    "direction": d.get("direction") or "—",
                    "entry_price": entry,
                    "exit_price": exit_p,
                    "current_price": exit_p,
                    "size": float(d.get("size") or 0),
                    "stake_usd": stake,
                    "pnl": round(pnl, 4),
                    "pnl_pct": round(pnl_pct, 2),
                    "trade_status": trade_status,
                    "close_reason": raw_reason,
                    "close_reason_label": reason_label,
                    "opened_at": opened_at,
                    "closed_at": closed_at,
                    "hours_held": round(hours_held, 1),
                    "is_paper": True,
                    "sl_proximity": 0,
                    "tp_proximity": 0,
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                })

        # ── 3) Trades without a matching position or closed record ──
        # (fallback for orphaned trade rows)
        existing_markets = {t["market_id"] for t in unified}
        orphan_rows = conn.execute("""
            SELECT t.*, m.question, m.market_type
            FROM trades t
            LEFT JOIN markets m ON t.market_id = m.id
            WHERE t.market_id NOT IN ({})
            ORDER BY t.created_at DESC LIMIT 50
        """.format(",".join(f"'{mid}'" for mid in existing_markets) if existing_markets else "'__none__'")).fetchall()
        for r in orphan_rows:
            d = dict(r)
            stake = float(d.get("stake_usd") or 0)
            status_raw = (d.get("status") or "").upper()
            is_exit = "EXIT" in status_raw or "SELL" == (d.get("side") or "").upper()
            # Parse exit reason from status field (e.g. "SIMULATED|STOP_LOSS")
            close_reason = ""
            if "|" in status_raw:
                parts = status_raw.split("|", 1)
                close_reason = parts[1].split(":")[0].strip() if len(parts) > 1 else ""
            reason_labels = {
                "STOP_LOSS": "🔴 Stop Loss Hit",
                "TAKE_PROFIT": "🟢 Take Profit Hit",
                "MARKET_RESOLVED": "📋 Market Resolved",
                "MAX_HOLDING": "⏰ Max Hold Time",
            }
            unified.append({
                "market_id": d.get("market_id", ""),
                "question": d.get("question") or d.get("market_id", ""),
                "market_type": d.get("market_type") or "—",
                "direction": d.get("side") or "—",
                "entry_price": float(d.get("price") or 0),
                "exit_price": None,
                "current_price": float(d.get("price") or 0),
                "size": float(d.get("size") or 0),
                "stake_usd": stake,
                "pnl": 0,
                "pnl_pct": 0,
                "trade_status": "ENTRY" if not is_exit else "CLOSED",
                "close_reason": close_reason,
                "close_reason_label": reason_labels.get(close_reason, close_reason or None),
                "opened_at": d.get("created_at", ""),
                "closed_at": None,
                "hours_held": 0,
                "is_paper": bool(d.get("dry_run", 1)),
                "sl_proximity": 0,
                "tp_proximity": 0,
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
            })

        # ── Sort: active first, then by date desc ────────────────
        def sort_key(t: dict) -> tuple:
            is_active = 0 if t["trade_status"] == "ACTIVE" else 1
            ts = t.get("closed_at") or t.get("opened_at") or ""
            return (is_active, ts)
        unified.sort(key=sort_key, reverse=False)
        # Active first, then closed newest first
        active = [t for t in unified if t["trade_status"] == "ACTIVE"]
        closed = [t for t in unified if t["trade_status"] != "ACTIVE"]
        closed.sort(key=lambda t: t.get("closed_at") or t.get("opened_at") or "", reverse=True)
        all_trades = active + closed

        # ── Summary stats ────────────────────────────────────────
        closed_trades = [t for t in all_trades if t["trade_status"] not in ("ACTIVE", "ENTRY")]
        active_trades = [t for t in all_trades if t["trade_status"] == "ACTIVE"]
        total_pnl = sum(t["pnl"] for t in closed_trades)
        winners = [t for t in closed_trades if t["pnl"] > 0]
        losers = [t for t in closed_trades if t["pnl"] < 0]
        tp_hits = sum(1 for t in closed_trades if t["trade_status"] == "TP_HIT")
        sl_hits = sum(1 for t in closed_trades if t["trade_status"] == "SL_HIT")
        resolved = sum(1 for t in closed_trades if t["trade_status"] == "RESOLVED")
        time_exits = sum(1 for t in closed_trades if t["trade_status"] == "TIME_EXIT")
        avg_hold = sum(t["hours_held"] for t in closed_trades) / max(len(closed_trades), 1)
        best_trade = max((t["pnl"] for t in closed_trades), default=0)
        worst_trade = min((t["pnl"] for t in closed_trades), default=0)
        avg_win = sum(t["pnl"] for t in winners) / max(len(winners), 1)
        avg_loss = sum(t["pnl"] for t in losers) / max(len(losers), 1)
        total_invested = sum(t["stake_usd"] for t in all_trades)

        return jsonify({
            "trades": all_trades[:200],
            "summary": {
                "total_trades": len(all_trades),
                "active_count": len(active_trades),
                "closed_count": len(closed_trades),
                "total_pnl": round(total_pnl, 2),
                "total_invested": round(total_invested, 2),
                "pnl_pct": round(total_pnl / total_invested * 100, 2) if total_invested > 0 else 0,
                "winners": len(winners),
                "losers": len(losers),
                "win_rate": round(len(winners) / max(len(closed_trades), 1) * 100, 1),
                "tp_hits": tp_hits,
                "sl_hits": sl_hits,
                "resolved": resolved,
                "time_exits": time_exits,
                "avg_hold_hours": round(avg_hold, 1),
                "best_trade": round(best_trade, 2),
                "worst_trade": round(worst_trade, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
            },
        })
    finally:
        conn.close()


# ─── API: Trade Detail ─────────────────────────────────────────────

@app.route("/api/trade-detail/<market_id>")
def api_trade_detail(market_id: str) -> Any:
    """Return comprehensive detail for a single trade — works for both
    active positions and closed/archived trades."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        cfg = _get_config()
        sl_pct = getattr(cfg.risk, "stop_loss_pct", 0.20)
        tp_pct = getattr(cfg.risk, "take_profit_pct", 0.30)
        max_hold_hours = getattr(cfg.risk, "max_holding_hours", 72.0)
        now = dt.datetime.now(dt.timezone.utc)

        result: dict = {"market_id": market_id}
        is_active = False

        # ── 1) Check active positions ────────────────────────────
        pos_row = conn.execute("""
            SELECT p.*, m.question AS mkt_question, m.market_type AS mkt_type,
                   m.category, m.volume, m.liquidity, m.end_date,
                   m.resolution_source
            FROM positions p
            LEFT JOIN markets m ON p.market_id = m.id
            WHERE p.market_id = ?
        """, (market_id,)).fetchone()

        if pos_row:
            is_active = True
            p = dict(pos_row)
            entry = float(p.get("entry_price") or 0)
            current = float(p.get("current_price") or entry)
            stake = float(p.get("stake_usd") or 0)
            pnl = float(p.get("pnl") or 0)
            pnl_pct = (pnl / stake * 100) if stake > 0 else 0.0
            direction = p.get("direction") or "—"

            # Duration
            hours_held = 0.0
            opened_at = p.get("opened_at", "")
            if opened_at:
                try:
                    opened = dt.datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                    hours_held = (now - opened).total_seconds() / 3600
                except (ValueError, TypeError):
                    pass

            # TP/SL proximity
            sl_proximity = min(abs(pnl_pct) / (sl_pct * 100), 1.0) if sl_pct > 0 and pnl_pct < 0 else 0.0
            tp_proximity = min(pnl_pct / (tp_pct * 100), 1.0) if tp_pct > 0 and pnl_pct > 0 else 0.0

            # TP/SL price levels
            if direction in ("BUY_YES", "BUY"):
                sl_price = entry * (1 - sl_pct) if sl_pct > 0 else None
                tp_price = entry * (1 + tp_pct) if tp_pct > 0 else None
            else:
                sl_price = entry * (1 + sl_pct) if sl_pct > 0 else None
                tp_price = entry * (1 - tp_pct) if tp_pct > 0 else None

            holding_pct = (hours_held / max_hold_hours * 100) if max_hold_hours > 0 else 0.0
            time_remaining = max(max_hold_hours - hours_held, 0) if max_hold_hours > 0 else None

            result.update({
                "trade_status": "ACTIVE",
                "close_reason": None,
                "close_reason_label": None,
                "question": p.get("mkt_question") or p.get("question") or market_id,
                "market_type": p.get("mkt_type") or p.get("market_type") or "—",
                "category": p.get("category") or "—",
                "direction": direction,
                "token_id": p.get("token_id") or "",
                "entry_price": round(entry, 4),
                "exit_price": None,
                "current_price": round(current, 4),
                "price_change": round(current - entry, 4),
                "price_change_pct": round(((current - entry) / entry * 100) if entry > 0 else 0, 2),
                "size": round(float(p.get("size") or 0), 2),
                "stake_usd": round(stake, 2),
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 2),
                "opened_at": opened_at,
                "closed_at": None,
                "hours_held": round(hours_held, 1),
                "holding_pct": round(holding_pct, 1),
                "max_holding_hours": max_hold_hours,
                "time_remaining_hours": round(time_remaining, 1) if time_remaining is not None else None,
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
                "sl_proximity": round(sl_proximity, 3),
                "tp_proximity": round(tp_proximity, 3),
                "sl_price": round(sl_price, 4) if sl_price is not None else None,
                "tp_price": round(tp_price, 4) if tp_price is not None else None,
                "sl_trigger_pnl_pct": round(-sl_pct * 100, 1),
                "tp_trigger_pnl_pct": round(tp_pct * 100, 1),
                "volume": round(float(p.get("volume") or 0), 2),
                "liquidity": round(float(p.get("liquidity") or 0), 2),
                "end_date": p.get("end_date") or "—",
                "resolution_source": p.get("resolution_source") or "—",
                "is_paper": True,
            })

        # ── 2) Check closed_positions ────────────────────────────
        if not is_active:
            cp_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='closed_positions'"
            ).fetchone()
            cp_row = None
            if cp_exists:
                cp_row = conn.execute("""
                    SELECT cp.*, m.question AS mkt_question, m.market_type AS mkt_type,
                           m.category, m.volume, m.liquidity, m.end_date,
                           m.resolution_source
                    FROM closed_positions cp
                    LEFT JOIN markets m ON cp.market_id = m.id
                    WHERE cp.market_id = ?
                    ORDER BY cp.closed_at DESC LIMIT 1
                """, (market_id,)).fetchone()

            if cp_row:
                c = dict(cp_row)
                entry = float(c.get("entry_price") or 0)
                exit_p = float(c.get("exit_price") or 0)
                stake = float(c.get("stake_usd") or 0)
                pnl = float(c.get("pnl") or 0)
                pnl_pct = (pnl / stake * 100) if stake > 0 else 0.0
                direction = c.get("direction") or "—"

                # Duration
                hours_held = 0.0
                opened_at = c.get("opened_at", "")
                closed_at = c.get("closed_at", "")
                if opened_at and closed_at:
                    try:
                        op = dt.datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                        cl = dt.datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                        hours_held = (cl - op).total_seconds() / 3600
                    except (ValueError, TypeError):
                        pass

                raw_reason = (c.get("close_reason") or "").upper()
                reason_labels = {
                    "STOP_LOSS": "🔴 Stop Loss Hit",
                    "TAKE_PROFIT": "🟢 Take Profit Hit",
                    "MARKET_RESOLVED": "📋 Market Resolved",
                    "MAX_HOLDING": "⏰ Max Hold Time",
                    "MANUAL": "👤 Manual Close",
                }
                status_map = {
                    "TAKE_PROFIT": "TP_HIT", "STOP_LOSS": "SL_HIT",
                    "MARKET_RESOLVED": "RESOLVED", "MAX_HOLDING": "TIME_EXIT",
                }
                trade_status = status_map.get(raw_reason, "CLOSED")

                # TP/SL price levels (for historical reference)
                if direction in ("BUY_YES", "BUY"):
                    sl_price = entry * (1 - sl_pct) if sl_pct > 0 else None
                    tp_price = entry * (1 + tp_pct) if tp_pct > 0 else None
                else:
                    sl_price = entry * (1 + sl_pct) if sl_pct > 0 else None
                    tp_price = entry * (1 - tp_pct) if tp_pct > 0 else None

                result.update({
                    "trade_status": trade_status,
                    "close_reason": raw_reason,
                    "close_reason_label": reason_labels.get(raw_reason, raw_reason or "Closed"),
                    "question": c.get("mkt_question") or c.get("question") or market_id,
                    "market_type": c.get("mkt_type") or c.get("market_type") or "—",
                    "category": c.get("category") or "—",
                    "direction": direction,
                    "token_id": c.get("token_id") or "",
                    "entry_price": round(entry, 4),
                    "exit_price": round(exit_p, 4),
                    "current_price": round(exit_p, 4),
                    "price_change": round(exit_p - entry, 4),
                    "price_change_pct": round(((exit_p - entry) / entry * 100) if entry > 0 else 0, 2),
                    "size": round(float(c.get("size") or 0), 2),
                    "stake_usd": round(stake, 2),
                    "pnl": round(pnl, 4),
                    "pnl_pct": round(pnl_pct, 2),
                    "opened_at": opened_at,
                    "closed_at": closed_at,
                    "hours_held": round(hours_held, 1),
                    "holding_pct": 100.0,
                    "max_holding_hours": max_hold_hours,
                    "time_remaining_hours": 0,
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                    "sl_proximity": 1.0 if raw_reason == "STOP_LOSS" else 0,
                    "tp_proximity": 1.0 if raw_reason == "TAKE_PROFIT" else 0,
                    "sl_price": round(sl_price, 4) if sl_price is not None else None,
                    "tp_price": round(tp_price, 4) if tp_price is not None else None,
                    "sl_trigger_pnl_pct": round(-sl_pct * 100, 1),
                    "tp_trigger_pnl_pct": round(tp_pct * 100, 1),
                    "volume": round(float(c.get("volume") or 0), 2),
                    "liquidity": round(float(c.get("liquidity") or 0), 2),
                    "end_date": c.get("end_date") or "—",
                    "resolution_source": c.get("resolution_source") or "—",
                    "is_paper": True,
                })
            else:
                # Fallback: try trades table
                t_row = conn.execute("""
                    SELECT t.*, m.question, m.market_type, m.category
                    FROM trades t
                    LEFT JOIN markets m ON t.market_id = m.id
                    WHERE t.market_id = ?
                    ORDER BY t.created_at DESC LIMIT 1
                """, (market_id,)).fetchone()
                if not t_row:
                    return jsonify({"error": "Trade not found"}), 404
                t = dict(t_row)
                result.update({
                    "trade_status": "ENTRY",
                    "close_reason": None,
                    "close_reason_label": None,
                    "question": t.get("question") or market_id,
                    "market_type": t.get("market_type") or "—",
                    "category": t.get("category") or "—",
                    "direction": t.get("side") or "—",
                    "token_id": t.get("token_id") or "",
                    "entry_price": round(float(t.get("price") or 0), 4),
                    "exit_price": None,
                    "current_price": round(float(t.get("price") or 0), 4),
                    "price_change": 0,
                    "price_change_pct": 0,
                    "size": round(float(t.get("size") or 0), 2),
                    "stake_usd": round(float(t.get("stake_usd") or 0), 2),
                    "pnl": 0, "pnl_pct": 0,
                    "opened_at": t.get("created_at", ""),
                    "closed_at": None,
                    "hours_held": 0, "holding_pct": 0,
                    "max_holding_hours": max_hold_hours,
                    "time_remaining_hours": None,
                    "sl_pct": sl_pct, "tp_pct": tp_pct,
                    "sl_proximity": 0, "tp_proximity": 0,
                    "sl_price": None, "tp_price": None,
                    "sl_trigger_pnl_pct": round(-sl_pct * 100, 1),
                    "tp_trigger_pnl_pct": round(tp_pct * 100, 1),
                    "volume": 0, "liquidity": 0,
                    "end_date": "—", "resolution_source": "—",
                    "is_paper": bool(t.get("dry_run", 1)),
                })

        # ── 3) Latest forecast for this market ───────────────────
        fc_row = conn.execute("""
            SELECT model_probability, implied_probability, edge,
                   confidence_level, evidence_quality, num_sources,
                   decision, reasoning, created_at
            FROM forecasts
            WHERE market_id = ?
            ORDER BY created_at DESC LIMIT 1
        """, (market_id,)).fetchone()
        result["forecast"] = dict(fc_row) if fc_row else None

        # ── 4) All trade records for this market ─────────────────
        trade_rows = conn.execute("""
            SELECT id, order_id, side, price, size, stake_usd, status,
                   dry_run, created_at
            FROM trades
            WHERE market_id = ?
            ORDER BY created_at ASC
        """, (market_id,)).fetchall()
        result["trade_records"] = [dict(r) for r in trade_rows]

        # ── 5) Decision log entries for this market ──────────────
        dl_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decision_log'"
        ).fetchone()
        decisions: list[dict] = []
        if dl_exists:
            dl_rows = conn.execute("""
                SELECT id, market_id, decision, stage, details,
                       integrity_hash, timestamp
                FROM decision_log
                WHERE market_id = ?
                ORDER BY timestamp ASC
                LIMIT 50
            """, (market_id,)).fetchall()
            decisions = [dict(r) for r in dl_rows]
        result["decisions"] = decisions

        # ── 6) Performance log entries ───────────────────────────
        perf_rows = conn.execute("""
            SELECT * FROM performance_log
            WHERE market_id = ?
            ORDER BY resolved_at DESC LIMIT 5
        """, (market_id,)).fetchall()
        result["performance"] = [dict(r) for r in perf_rows]

        # Polymarket link
        result["polymarket_url"] = f"https://polymarket.com/event/{market_id}"

        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─── API: Risk Monitor ─────────────────────────────────────────────

@app.route("/api/risk")
def api_risk() -> Any:
    cfg = _get_config()
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        today = dt.date.today().isoformat()
        # Daily loss
        daily_trades = conn.execute(
            "SELECT COALESCE(SUM(stake_usd), 0) as total FROM trades WHERE date(created_at) = ?",
            (today,)
        ).fetchone()
        daily_exposure = float(daily_trades["total"]) if daily_trades else 0.0

        # Open positions count
        pos_count = conn.execute("SELECT COUNT(*) as cnt FROM positions").fetchone()
        open_positions = int(pos_count["cnt"]) if pos_count else 0

        # Recent forecasts average evidence quality
        recent = conn.execute(
            "SELECT evidence_quality FROM forecasts ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        recent_eq = [r["evidence_quality"] for r in recent if r["evidence_quality"]]
        avg_recent_eq = sum(recent_eq) / len(recent_eq) if recent_eq else 0.0

        return jsonify({
            "kill_switch": cfg.risk.kill_switch,
            "limits": {
                "max_daily_loss": cfg.risk.max_daily_loss,
                "max_open_positions": cfg.risk.max_open_positions,
                "max_stake_per_market": cfg.risk.max_stake_per_market,
                "max_bankroll_fraction": cfg.risk.max_bankroll_fraction,
                "min_edge": cfg.risk.min_edge,
                "min_liquidity": cfg.risk.min_liquidity,
                "max_spread": cfg.risk.max_spread,
                "kelly_fraction": cfg.risk.kelly_fraction,
            },
            "current": {
                "daily_exposure": daily_exposure,
                "open_positions": open_positions,
                "avg_evidence_quality": round(avg_recent_eq, 3),
                "daily_loss_pct": round(
                    (daily_exposure / cfg.risk.max_daily_loss * 100) if cfg.risk.max_daily_loss > 0 else 0, 1
                ),
                "positions_pct": round(
                    (open_positions / cfg.risk.max_open_positions * 100) if cfg.risk.max_open_positions > 0 else 0, 1
                ),
            },
            "execution": {
                "dry_run": cfg.execution.dry_run,
                "live_trading_enabled": is_live_trading_enabled(),
                "slippage_tolerance": cfg.execution.slippage_tolerance,
            },
            "forecasting": {
                "min_evidence_quality": cfg.forecasting.min_evidence_quality,
                "low_evidence_penalty": cfg.forecasting.low_evidence_penalty,
                "llm_model": cfg.forecasting.llm_model,
            },
        })
    finally:
        conn.close()


# ─── API: Market Type Breakdown ────────────────────────────────────

@app.route("/api/market-types")
def api_market_types() -> Any:
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute("""
            SELECT market_type, COUNT(*) as count,
                   AVG(evidence_quality) as avg_eq,
                   AVG(edge) as avg_edge
            FROM forecasts
            GROUP BY market_type
        """).fetchall()
        types = [dict(r) for r in rows]
        return jsonify({"market_types": types})
    finally:
        conn.close()


# ─── API: Performance Over Time ────────────────────────────────────

@app.route("/api/performance")
def api_performance() -> Any:
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Daily forecast counts and avg edge
        rows = conn.execute("""
            SELECT date(created_at) as day,
                   COUNT(*) as forecasts,
                   SUM(CASE WHEN decision = 'TRADE' THEN 1 ELSE 0 END) as trades,
                   AVG(edge) as avg_edge,
                   AVG(evidence_quality) as avg_eq
            FROM forecasts
            GROUP BY date(created_at)
            ORDER BY day DESC
            LIMIT 30
        """).fetchall()
        daily = [dict(r) for r in rows]

        # Daily trade volume
        trade_rows = conn.execute("""
            SELECT date(created_at) as day,
                   COUNT(*) as count,
                   SUM(stake_usd) as volume
            FROM trades
            GROUP BY date(created_at)
            ORDER BY day DESC
            LIMIT 30
        """).fetchall()
        trade_daily = [dict(r) for r in trade_rows]

        return jsonify({
            "daily_forecasts": daily,
            "daily_trades": trade_daily,
        })
    finally:
        conn.close()


# ─── API: System Metrics ───────────────────────────────────────────

@app.route("/api/metrics")
def api_metrics() -> Any:
    snapshot = metrics.snapshot()
    return jsonify(snapshot)


# ─── API: Drawdown State ──────────────────────────────────────────

@app.route("/api/drawdown")
def api_drawdown() -> Any:
    cfg = _get_config()
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        row = conn.execute(
            "SELECT value FROM engine_state WHERE key = 'drawdown'"
        ).fetchone()
        if row:
            dd = json.loads(row["value"])
            return jsonify({
                "peak_equity": dd.get("peak_equity", cfg.risk.bankroll),
                "current_equity": dd.get("current_equity", cfg.risk.bankroll),
                "drawdown_pct": dd.get("drawdown_pct", 0.0),
                "heat_level": dd.get("heat_level", 0),
                "kelly_multiplier": dd.get("kelly_multiplier", 1.0),
                "is_killed": dd.get("is_killed", False),
                "kill_switch_pct": cfg.drawdown.max_drawdown_pct,
                "warning_pct": cfg.drawdown.warning_drawdown_pct,
                "critical_pct": cfg.drawdown.critical_drawdown_pct,
                "max_drawdown_pct": cfg.drawdown.max_drawdown_pct,
            })
    except Exception:
        pass
    finally:
        conn.close()
    # Fallback when engine hasn't started yet
    return jsonify({
        "peak_equity": cfg.risk.bankroll,
        "current_equity": cfg.risk.bankroll,
        "drawdown_pct": 0.0,
        "heat_level": 0,
        "kelly_multiplier": 1.0,
        "is_killed": False,
        "kill_switch_pct": cfg.drawdown.max_drawdown_pct,
        "warning_pct": cfg.drawdown.warning_drawdown_pct,
        "critical_pct": cfg.drawdown.critical_drawdown_pct,
        "max_drawdown_pct": cfg.drawdown.max_drawdown_pct,
    })


# ─── API: Portfolio Risk ─────────────────────────────────────────

@app.route("/api/portfolio-risk")
def api_portfolio_risk() -> Any:
    cfg = _get_config()
    return jsonify({
        "max_exposure_per_category": cfg.portfolio.max_category_exposure_pct,
        "max_exposure_per_event": cfg.portfolio.max_single_event_exposure_pct,
        "max_correlated_positions": cfg.portfolio.max_correlated_positions,
        "correlation_threshold": cfg.portfolio.correlation_similarity_threshold,
        "category_exposures": {},
        "event_exposures": {},
        "is_healthy": True,
        "violations": [],
    })


# ─── API: Engine Status ─────────────────────────────────────────

@app.route("/api/engine-status")
def api_engine() -> Any:
    cfg = _get_config()

    # Prefer live in-process engine status
    engine_running = (
        _engine_instance is not None
        and _engine_instance.is_running
        and _engine_thread is not None
        and _engine_thread.is_alive()
    )

    if engine_running and _engine_instance is not None:
        try:
            live = _engine_instance.get_status()
            return jsonify({
                "running": True,
                "scan_interval_minutes": cfg.engine.scan_interval_minutes,
                "max_markets_per_cycle": cfg.engine.max_markets_per_cycle,
                "auto_start": cfg.engine.auto_start,
                "paper_mode": cfg.engine.paper_mode,
                "cycles": live.get("cycle_count", 0),
                "last_cycle": live.get("last_cycle"),
                "live_trading": live.get("live_trading", False),
                "positions": live.get("positions", 0),
                "uptime_secs": round(time.time() - _engine_started_at, 0),
                "engine_embedded": True,
                "engine_error": _engine_error,
            })
        except Exception:
            pass

    # Fall back to DB-persisted state
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        row = conn.execute(
            "SELECT value FROM engine_state WHERE key = 'engine_status'"
        ).fetchone()
        if row:
            state = json.loads(row["value"])
            return jsonify({
                "running": state.get("running", False),
                "scan_interval_minutes": state.get("scan_interval_minutes", cfg.engine.scan_interval_minutes),
                "max_markets_per_cycle": state.get("max_markets_per_cycle", cfg.engine.max_markets_per_cycle),
                "auto_start": state.get("auto_start", cfg.engine.auto_start),
                "paper_mode": state.get("paper_mode", cfg.engine.paper_mode),
                "cycles": state.get("cycle_count", 0),
                "last_cycle": state.get("last_cycle"),
                "live_trading": state.get("live_trading", False),
                "positions": state.get("positions", 0),
                "engine_embedded": True,
                "engine_error": _engine_error,
            })
    except Exception:
        pass
    finally:
        conn.close()
    # Fallback when engine hasn't started yet
    return jsonify({
        "running": False,
        "scan_interval_minutes": cfg.engine.scan_interval_minutes,
        "max_markets_per_cycle": cfg.engine.max_markets_per_cycle,
        "auto_start": cfg.engine.auto_start,
        "paper_mode": cfg.engine.paper_mode,
        "cycles": 0,
        "last_cycle": None,
        "engine_embedded": True,
        "engine_error": _engine_error,
    })


# ─── API: Engine Start / Stop ───────────────────────────────────

@app.route("/api/engine/start", methods=["POST"])
def api_engine_start() -> Any:
    """Start the trading engine in a background thread."""
    cfg = _get_config()
    if _engine_thread and _engine_thread.is_alive():
        return jsonify({"ok": False, "message": "Engine is already running."})
    ok = _start_engine(cfg)
    return jsonify({"ok": ok, "message": "Engine starting…" if ok else "Failed to start."})


@app.route("/api/engine/stop", methods=["POST"])
def api_engine_stop() -> Any:
    """Gracefully stop the trading engine."""
    ok = _stop_engine()
    return jsonify({"ok": ok, "message": "Engine stopping…" if ok else "Engine is not running."})


# ─── API: Alerts History ────────────────────────────────────────

@app.route("/api/alerts")
def api_alerts() -> Any:
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Check if alerts_log table exists
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts_log'"
        ).fetchone()
        if not table_exists:
            return jsonify({"alerts": []})

        rows = conn.execute(
            "SELECT * FROM alerts_log ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        alerts = [dict(r) for r in rows]
        return jsonify({"alerts": alerts})
    finally:
        conn.close()


# ─── API: Candidates Log ────────────────────────────────────────

@app.route("/api/candidates")
def api_candidates() -> Any:
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Check if candidates table exists
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='candidates'"
        ).fetchone()
        if not table_exists:
            return jsonify({"candidates": []})

        limit = request.args.get("limit", 100, type=int)
        rows = conn.execute(
            "SELECT * FROM candidates ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        candidates = [dict(r) for r in rows]
        return jsonify({"candidates": candidates})
    finally:
        conn.close()


# ─── API: Filter Stats ──────────────────────────────────────────

@app.route("/api/filter-stats")
def api_filter_stats() -> Any:
    """Return the last pre-research filter stats from the engine."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        row = conn.execute(
            "SELECT value FROM engine_state WHERE key = 'engine_status'"
        ).fetchone()
        if not row:
            return jsonify({"filter_stats": None, "research_cache_size": 0})
        state = json.loads(row["value"])
        return jsonify({
            "filter_stats": state.get("filter_stats"),
            "research_cache_size": state.get("research_cache_size", 0),
        })
    finally:
        conn.close()


# ─── API: Decision Log ──────────────────────────────────────────

@app.route("/api/decision-log")
def api_decision_log() -> Any:
    """Rich decision log joining candidates with forecast evidence & reasoning."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        limit = request.args.get("limit", 50, type=int)
        cycle_id = request.args.get("cycle", None, type=int)

        # Get candidates (most recent first)
        if cycle_id is not None:
            cand_rows = conn.execute(
                "SELECT * FROM candidates WHERE cycle_id = ? ORDER BY created_at DESC",
                (cycle_id,),
            ).fetchall()
        else:
            cand_rows = conn.execute(
                "SELECT * FROM candidates ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        entries: list[dict[str, Any]] = []
        for c in cand_rows:
            cd = dict(c)
            market_id = cd.get("market_id", "")

            # Find matching forecast (closest in time)
            f_row = conn.execute(
                """SELECT * FROM forecasts
                   WHERE market_id = ?
                   ORDER BY ABS(julianday(created_at) - julianday(?)) ASC
                   LIMIT 1""",
                (market_id, cd.get("created_at", "")),
            ).fetchone()

            # Parse forecast enrichment data
            evidence_bullets: list[dict] = []
            reasoning = ""
            invalidation_triggers: list[str] = []
            research_evidence: dict[str, Any] = {}
            if f_row:
                fd = dict(f_row)
                reasoning = fd.get("reasoning", "") or ""

                # Parse rich research evidence (original sources with real URLs)
                try:
                    raw_research = fd.get("research_evidence_json", "{}") or "{}"
                    research_evidence = json.loads(raw_research)
                    if not isinstance(research_evidence, dict):
                        research_evidence = {}
                except (json.JSONDecodeError, TypeError):
                    research_evidence = {}

                # Use research evidence bullets if available (real citations),
                # fall back to LLM evidence
                if research_evidence.get("evidence"):
                    evidence_bullets = research_evidence["evidence"]
                else:
                    try:
                        evidence_bullets = json.loads(
                            fd.get("evidence_json", "[]") or "[]"
                        )
                        if not isinstance(evidence_bullets, list):
                            evidence_bullets = []
                    except (json.JSONDecodeError, TypeError):
                        evidence_bullets = []

                try:
                    invalidation_triggers = json.loads(
                        fd.get("invalidation_triggers_json", "[]") or "[]"
                    )
                    if not isinstance(invalidation_triggers, list):
                        invalidation_triggers = []
                except (json.JSONDecodeError, TypeError):
                    invalidation_triggers = []

            # Look up market metadata
            m_row = conn.execute(
                "SELECT volume, liquidity, end_date, resolution_source, category "
                "FROM markets WHERE id = ? LIMIT 1",
                (market_id,),
            ).fetchone()
            market_meta = dict(m_row) if m_row else {}

            # Build pipeline stages
            stages: list[dict[str, Any]] = []

            # Stage 1: Discovery & Filter
            stages.append({
                "name": "Discovery & Filter",
                "icon": "🔍",
                "status": "passed",
                "details": {
                    "market_type": cd.get("market_type", "UNKNOWN"),
                    "volume": market_meta.get("volume"),
                    "liquidity": market_meta.get("liquidity"),
                    "end_date": market_meta.get("end_date"),
                    "resolution_source": market_meta.get("resolution_source", ""),
                    "category": market_meta.get("category", ""),
                },
            })

            # Stage 1.5: Classification (from market classifier)
            classification_data = research_evidence.get("classification", {})
            if classification_data:
                stages.append({
                    "name": "Classification",
                    "icon": "🏷️",
                    "status": "passed" if classification_data.get("worth_researching", True) else "blocked",
                    "details": {
                        "category": classification_data.get("category", "UNKNOWN"),
                        "subcategory": classification_data.get("subcategory", "unknown"),
                        "researchability": classification_data.get("researchability", 0),
                        "researchability_reasons": classification_data.get("researchability_reasons", []),
                        "primary_sources": classification_data.get("primary_sources", []),
                        "search_strategy": classification_data.get("search_strategy", ""),
                        "recommended_queries": classification_data.get("recommended_queries", 4),
                        "worth_researching": classification_data.get("worth_researching", True),
                        "confidence": classification_data.get("confidence", 0),
                        "tags": classification_data.get("tags", []),
                    },
                })

            # Stage 2: Research
            research_status = "passed" if cd.get("num_sources", 0) > 0 else "skipped"
            research_details: dict[str, Any] = {
                "num_sources": cd.get("num_sources", 0),
                "evidence_quality": cd.get("evidence_quality", 0),
                "evidence_bullets": evidence_bullets[:5],
            }
            # Add rich research package data when available
            if research_evidence:
                research_details["summary"] = research_evidence.get("summary", "")
                research_details["contradictions"] = research_evidence.get(
                    "contradictions", []
                )
                research_details["quality_breakdown"] = research_evidence.get(
                    "independent_quality", {}
                )
                research_details["llm_quality_score"] = research_evidence.get(
                    "llm_quality_score", 0
                )
            stages.append({
                "name": "Research",
                "icon": "📚",
                "status": research_status,
                "details": research_details,
            })

            # Stage 3: Forecast
            has_forecast = cd.get("model_prob", 0) > 0
            stages.append({
                "name": "Forecast",
                "icon": "🎯",
                "status": "passed" if has_forecast else "skipped",
                "details": {
                    "implied_prob": cd.get("implied_prob", 0),
                    "model_prob": cd.get("model_prob", 0),
                    "edge": cd.get("edge", 0),
                    "confidence": cd.get("confidence", ""),
                    "reasoning": reasoning,
                    "invalidation_triggers": invalidation_triggers,
                },
            })

            # Stage 4: Risk Check
            decision = (cd.get("decision") or "").upper()
            reasons = cd.get("decision_reasons", "") or ""
            risk_passed = decision == "TRADE"
            stages.append({
                "name": "Risk Check",
                "icon": "⚠️",
                "status": "passed" if risk_passed else "blocked",
                "details": {
                    "decision": decision,
                    "reasons": reasons,
                    "violations": (
                        [r.strip() for r in reasons.split(";") if r.strip()]
                        if not risk_passed and reasons
                        else []
                    ),
                },
            })

            # Stage 5: Execution
            if decision == "TRADE":
                stages.append({
                    "name": "Execution",
                    "icon": "⚡",
                    "status": "executed",
                    "details": {
                        "stake_usd": cd.get("stake_usd", 0),
                        "order_status": cd.get("order_status", ""),
                    },
                })

            # ── Compute Decision Intelligence Score & Grade ──
            edge_abs = abs(cd.get("edge", 0))
            eq = cd.get("evidence_quality", 0)
            n_src = cd.get("num_sources", 0)
            researchability = classification_data.get("researchability", 0)
            conf_text = (cd.get("confidence", "") or "").lower()
            conf_num = (
                0.9 if conf_text == "high" else 0.6 if conf_text == "medium"
                else 0.3 if conf_text == "low" else 0.0
            )
            # Composite score 0-100
            di_score = round(min(100, (
                edge_abs * 200 * 0.30          # edge component (30%)
                + eq * 100 * 0.25              # evidence quality (25%)
                + min(n_src / 5, 1) * 100 * 0.15  # source coverage (15%)
                + researchability * 0.15       # researchability (15%)
                + conf_num * 100 * 0.15        # confidence (15%)
            )))
            di_grade = (
                "A+" if di_score >= 90 else "A" if di_score >= 80
                else "B+" if di_score >= 72 else "B" if di_score >= 64
                else "C+" if di_score >= 56 else "C" if di_score >= 48
                else "D" if di_score >= 35 else "F"
            )

            # ── Decision Reasons (human-readable) ──
            decision_reasons_list: list[str] = []
            if decision == "TRADE":
                decision_reasons_list.append(
                    f"Edge of {edge_abs*100:.1f}% exceeds minimum threshold"
                )
                if eq > 0.5:
                    decision_reasons_list.append(
                        f"Evidence quality ({eq:.2f}) indicates reliable data"
                    )
                if conf_text in ("high", "medium"):
                    decision_reasons_list.append(
                        f"Model confidence is {conf_text}"
                    )
            else:
                if reasons:
                    for r in reasons.split(";"):
                        r = r.strip()
                        if r:
                            decision_reasons_list.append(r)
                if edge_abs * 100 < 5 and not decision_reasons_list:
                    decision_reasons_list.append("Insufficient edge to justify trade")
                if not decision_reasons_list:
                    decision_reasons_list.append("Did not pass risk checks")

            # ── Pipeline completeness ──
            stages_passed = sum(
                1 for s in stages
                if s["status"] in ("passed", "executed")
            )
            pipeline_completeness = round(stages_passed / max(len(stages), 1) * 100)

            entries.append({
                "cycle_id": cd.get("cycle_id"),
                "market_id": market_id,
                "question": cd.get("question", ""),
                "market_type": cd.get("market_type", ""),
                "decision": decision,
                "implied_prob": cd.get("implied_prob", 0),
                "model_prob": cd.get("model_prob", 0),
                "edge": cd.get("edge", 0),
                "evidence_quality": cd.get("evidence_quality", 0),
                "num_sources": cd.get("num_sources", 0),
                "confidence": cd.get("confidence", ""),
                "stake_usd": cd.get("stake_usd", 0),
                "created_at": cd.get("created_at", ""),
                "stages": stages,
                "reasoning": reasoning,
                "evidence_bullets": evidence_bullets[:5],
                "invalidation_triggers": invalidation_triggers,
                "research_summary": research_evidence.get("summary", ""),
                "contradictions": research_evidence.get("contradictions", []),
                "quality_breakdown": research_evidence.get(
                    "independent_quality", {}
                ),
                # ── Enhanced Intelligence Fields ──
                "di_score": di_score,
                "di_grade": di_grade,
                "decision_reasons_list": decision_reasons_list,
                "pipeline_completeness": pipeline_completeness,
                "category": classification_data.get("category", ""),
                "subcategory": classification_data.get("subcategory", ""),
                "researchability": researchability,
                "tags": classification_data.get("tags", []),
                "search_strategy": classification_data.get("search_strategy", ""),
                "worth_researching": classification_data.get("worth_researching", True),
            })

        # Gather unique cycle IDs for the cycle selector
        cycle_rows = conn.execute(
            "SELECT DISTINCT cycle_id FROM candidates ORDER BY cycle_id DESC LIMIT 20"
        ).fetchall()
        cycles = [r["cycle_id"] for r in cycle_rows if r["cycle_id"] is not None]

        # ── Aggregate Statistics ──────────────────────────────────
        total = len(entries)
        trade_count = sum(1 for e in entries if e["decision"] == "TRADE")
        no_trade_count = sum(1 for e in entries if e["decision"] == "NO TRADE")
        skip_count = total - trade_count - no_trade_count

        # Averages (only for entries with actual data)
        edges = [e["edge"] for e in entries if e["edge"] != 0]
        di_scores = [e["di_score"] for e in entries if e["di_score"] > 0]
        eq_vals = [e["evidence_quality"] for e in entries if e["evidence_quality"] > 0]
        pipeline_vals = [e["pipeline_completeness"] for e in entries]
        avg_edge = (sum(edges) / len(edges)) if edges else 0
        avg_di_score = (sum(di_scores) / len(di_scores)) if di_scores else 0
        avg_eq = (sum(eq_vals) / len(eq_vals)) if eq_vals else 0
        avg_pipeline = (sum(pipeline_vals) / len(pipeline_vals)) if pipeline_vals else 0

        # Grade distribution
        grade_dist: dict[str, int] = {}
        for e in entries:
            g = e.get("di_grade", "F")
            grade_dist[g] = grade_dist.get(g, 0) + 1

        # Average grade as a number for display
        grade_num_map = {"A+": 97, "A": 85, "B+": 76, "B": 68, "C+": 60, "C": 52, "D": 40, "F": 20}
        grade_nums = [grade_num_map.get(e.get("di_grade", "F"), 0) for e in entries]
        avg_grade_num = (sum(grade_nums) / len(grade_nums)) if grade_nums else 0
        avg_grade = (
            "A+" if avg_grade_num >= 90 else "A" if avg_grade_num >= 80
            else "B+" if avg_grade_num >= 72 else "B" if avg_grade_num >= 64
            else "C+" if avg_grade_num >= 56 else "C" if avg_grade_num >= 48
            else "D" if avg_grade_num >= 35 else "F"
        )

        # Category breakdown
        category_dist: dict[str, dict[str, int]] = {}
        for e in entries:
            cat = e.get("category") or "UNKNOWN"
            if cat not in category_dist:
                category_dist[cat] = {"total": 0, "trades": 0, "no_trades": 0}
            category_dist[cat]["total"] += 1
            if e["decision"] == "TRADE":
                category_dist[cat]["trades"] += 1
            elif e["decision"] == "NO TRADE":
                category_dist[cat]["no_trades"] += 1

        # Top category
        top_category = max(category_dist, key=lambda k: category_dist[k]["total"]) if category_dist else "—"

        # Edge distribution (histogram buckets)
        edge_buckets = {"<-10%": 0, "-10 to -5%": 0, "-5 to 0%": 0, "0 to 5%": 0, "5 to 10%": 0, "10 to 20%": 0, ">20%": 0}
        for e in entries:
            ev = (e["edge"] or 0) * 100
            if ev < -10: edge_buckets["<-10%"] += 1
            elif ev < -5: edge_buckets["-10 to -5%"] += 1
            elif ev < 0: edge_buckets["-5 to 0%"] += 1
            elif ev < 5: edge_buckets["0 to 5%"] += 1
            elif ev < 10: edge_buckets["5 to 10%"] += 1
            elif ev < 20: edge_buckets["10 to 20%"] += 1
            else: edge_buckets[">20%"] += 1

        # Decision timeline (group by hour)
        from collections import defaultdict
        timeline_data: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "trades": 0, "no_trades": 0})
        for e in entries:
            ts = e.get("created_at", "")
            if ts:
                hour_key = ts[:13]  # "2026-02-12T14" → group by hour
                timeline_data[hour_key]["total"] += 1
                if e["decision"] == "TRADE":
                    timeline_data[hour_key]["trades"] += 1
                elif e["decision"] == "NO TRADE":
                    timeline_data[hour_key]["no_trades"] += 1
        decision_timeline = [
            {"hour": k, "total": v["total"], "trades": v["trades"], "no_trades": v["no_trades"]}
            for k, v in sorted(timeline_data.items())
        ]

        # Pipeline funnel: how many pass each stage
        stage_names_order = ["Discovery & Filter", "Classification", "Research", "Forecast", "Risk Check", "Execution"]
        funnel_data: list[dict[str, Any]] = []
        for stage_name in stage_names_order:
            passed = 0
            blocked = 0
            skipped = 0
            for e in entries:
                for s in e.get("stages", []):
                    if s["name"] == stage_name:
                        if s["status"] in ("passed", "executed"):
                            passed += 1
                        elif s["status"] == "blocked":
                            blocked += 1
                        else:
                            skipped += 1
            if passed + blocked + skipped > 0:
                funnel_data.append({
                    "stage": stage_name,
                    "passed": passed,
                    "blocked": blocked,
                    "skipped": skipped,
                    "total": passed + blocked + skipped,
                })

        # Outcome tracking — join with trades to see if TRADE decisions were profitable
        outcomes: list[dict[str, Any]] = []
        try:
            for e in entries:
                if e["decision"] == "TRADE" and e.get("market_id"):
                    t_row = conn.execute(
                        "SELECT pnl, status, current_price, entry_price FROM trades WHERE market_id = ? ORDER BY created_at DESC LIMIT 1",
                        (e["market_id"],),
                    ).fetchone()
                    if t_row:
                        td = dict(t_row)
                        outcomes.append({
                            "market_id": e["market_id"],
                            "question": e.get("question", "")[:60],
                            "pnl": td.get("pnl", 0),
                            "status": td.get("status", ""),
                            "entry_price": td.get("entry_price", 0),
                            "current_price": td.get("current_price", 0),
                        })
        except Exception:
            pass  # trades table may not exist or have different schema

        stats = {
            "total": total,
            "trade_count": trade_count,
            "no_trade_count": no_trade_count,
            "skip_count": skip_count,
            "trade_rate": round(trade_count / max(total, 1) * 100, 1),
            "avg_edge": round(avg_edge * 100, 2),
            "avg_di_score": round(avg_di_score, 1),
            "avg_eq": round(avg_eq, 3),
            "avg_pipeline": round(avg_pipeline, 1),
            "avg_grade": avg_grade,
            "top_category": top_category,
            "grade_distribution": grade_dist,
            "category_distribution": category_dist,
            "edge_buckets": edge_buckets,
            "decision_timeline": decision_timeline,
            "funnel": funnel_data,
            "outcomes": outcomes,
            "unique_categories": sorted(category_dist.keys()),
        }

        # ── Enhanced Decision Intelligence Analytics ──────────

        # DI-A: Cycle Trend — avg DI score, edge, trade rate per cycle
        cycle_trend: list = []
        try:
            cycle_groups: dict = {}
            for e in entries:
                cid = e.get("cycle_id")
                if cid is None:
                    continue
                if cid not in cycle_groups:
                    cycle_groups[cid] = {"scores": [], "edges": [], "trades": 0, "total": 0, "eqs": []}
                cycle_groups[cid]["scores"].append(e.get("di_score", 0))
                cycle_groups[cid]["edges"].append(abs(e.get("edge", 0)) * 100)
                cycle_groups[cid]["eqs"].append(e.get("evidence_quality", 0))
                cycle_groups[cid]["total"] += 1
                if e.get("decision") == "TRADE":
                    cycle_groups[cid]["trades"] += 1
            for cid in sorted(cycle_groups.keys()):
                cg = cycle_groups[cid]
                cycle_trend.append({
                    "cycle_id": cid,
                    "avg_score": round(sum(cg["scores"]) / max(len(cg["scores"]), 1), 1),
                    "avg_edge": round(sum(cg["edges"]) / max(len(cg["edges"]), 1), 2),
                    "avg_eq": round(sum(cg["eqs"]) / max(len(cg["eqs"]), 1), 3),
                    "trade_rate": round(cg["trades"] / max(cg["total"], 1) * 100, 1),
                    "count": cg["total"],
                })
        except Exception:
            pass
        stats["cycle_trend"] = cycle_trend

        # DI-B: Confidence Calibration — accuracy per confidence level
        # For each confidence level, check: of those marked TRADE, how many had positive outcomes?
        # Also include edge accuracy: avg predicted edge vs avg actual PnL
        confidence_calibration: list = []
        try:
            conf_buckets: dict = {}
            for e in entries:
                conf = (e.get("confidence") or "none").lower()
                if conf not in conf_buckets:
                    conf_buckets[conf] = {"total": 0, "trades": 0, "no_trades": 0,
                                          "avg_edge": [], "avg_score": [],
                                          "positive_outcomes": 0, "negative_outcomes": 0}
                conf_buckets[conf]["total"] += 1
                conf_buckets[conf]["avg_edge"].append(abs(e.get("edge", 0)) * 100)
                conf_buckets[conf]["avg_score"].append(e.get("di_score", 0))
                if e.get("decision") == "TRADE":
                    conf_buckets[conf]["trades"] += 1
                    # Check outcome
                    oc = next((o for o in outcomes if o["market_id"] == e.get("market_id")), None)
                    if oc:
                        if (oc.get("pnl") or 0) > 0:
                            conf_buckets[conf]["positive_outcomes"] += 1
                        else:
                            conf_buckets[conf]["negative_outcomes"] += 1
                else:
                    conf_buckets[conf]["no_trades"] += 1

            for level in ["high", "medium", "low", "none"]:
                if level not in conf_buckets:
                    continue
                cb = conf_buckets[level]
                total_outcomes = cb["positive_outcomes"] + cb["negative_outcomes"]
                accuracy = round(cb["positive_outcomes"] / max(total_outcomes, 1) * 100, 1) if total_outcomes > 0 else None
                confidence_calibration.append({
                    "level": level.upper(),
                    "total": cb["total"],
                    "trades": cb["trades"],
                    "no_trades": cb["no_trades"],
                    "avg_edge": round(sum(cb["avg_edge"]) / max(len(cb["avg_edge"]), 1), 2),
                    "avg_score": round(sum(cb["avg_score"]) / max(len(cb["avg_score"]), 1), 1),
                    "positive_outcomes": cb["positive_outcomes"],
                    "negative_outcomes": cb["negative_outcomes"],
                    "accuracy": accuracy,
                })
        except Exception:
            pass
        stats["confidence_calibration"] = confidence_calibration

        # DI-C: Category Performance Ranking
        category_performance: list = []
        try:
            cat_perf: dict = {}
            for e in entries:
                cat = e.get("category") or "UNKNOWN"
                if cat not in cat_perf:
                    cat_perf[cat] = {"edges": [], "scores": [], "trades": 0,
                                     "total": 0, "eqs": [], "wins": 0, "losses": 0}
                cat_perf[cat]["edges"].append(abs(e.get("edge", 0)) * 100)
                cat_perf[cat]["scores"].append(e.get("di_score", 0))
                cat_perf[cat]["eqs"].append(e.get("evidence_quality", 0))
                cat_perf[cat]["total"] += 1
                if e.get("decision") == "TRADE":
                    cat_perf[cat]["trades"] += 1
                    oc = next((o for o in outcomes if o["market_id"] == e.get("market_id")), None)
                    if oc and (oc.get("pnl") or 0) > 0:
                        cat_perf[cat]["wins"] += 1
                    elif oc:
                        cat_perf[cat]["losses"] += 1
            for cat, cp in sorted(cat_perf.items(), key=lambda x: sum(x[1]["scores"]) / max(len(x[1]["scores"]), 1), reverse=True):
                wr = round(cp["wins"] / max(cp["wins"] + cp["losses"], 1) * 100, 1)
                category_performance.append({
                    "category": cat,
                    "total": cp["total"],
                    "trades": cp["trades"],
                    "avg_edge": round(sum(cp["edges"]) / max(len(cp["edges"]), 1), 2),
                    "avg_score": round(sum(cp["scores"]) / max(len(cp["scores"]), 1), 1),
                    "avg_eq": round(sum(cp["eqs"]) / max(len(cp["eqs"]), 1), 3),
                    "win_rate": wr,
                    "wins": cp["wins"],
                    "losses": cp["losses"],
                })
        except Exception:
            pass
        stats["category_performance"] = category_performance

        # DI-D: Missed Opportunities — NO TRADE decisions where edge was high
        missed_opportunities: list = []
        try:
            for e in entries:
                if e.get("decision") != "NO TRADE":
                    continue
                edge_pct = abs(e.get("edge", 0)) * 100
                di = e.get("di_score", 0)
                eq = e.get("evidence_quality", 0)
                # A "missed opportunity" has decent edge + reasonable score
                if edge_pct >= 8 or (edge_pct >= 5 and di >= 40):
                    reasons = e.get("decision_reasons_list", [])
                    missed_opportunities.append({
                        "market_id": e.get("market_id", ""),
                        "question": (e.get("question") or "")[:80],
                        "edge": round(edge_pct, 2),
                        "di_score": di,
                        "di_grade": e.get("di_grade", "F"),
                        "evidence_quality": eq,
                        "confidence": e.get("confidence", ""),
                        "category": e.get("category", ""),
                        "rejection_reasons": reasons[:3],
                        "created_at": e.get("created_at", ""),
                        "implied_prob": round((e.get("implied_prob") or 0) * 100, 1),
                        "model_prob": round((e.get("model_prob") or 0) * 100, 1),
                    })
            missed_opportunities.sort(key=lambda x: x["edge"], reverse=True)
            missed_opportunities = missed_opportunities[:15]
        except Exception:
            pass
        stats["missed_opportunities"] = missed_opportunities

        # DI-E: Research ROI — correlation between evidence quality and outcomes
        research_roi: dict = {}
        try:
            # Bucket decisions by evidence quality tier
            eq_tiers = {"excellent": [], "good": [], "fair": [], "poor": []}
            for e in entries:
                eq_val = e.get("evidence_quality", 0)
                edge_val = abs(e.get("edge", 0)) * 100
                if eq_val >= 0.7:
                    eq_tiers["excellent"].append(edge_val)
                elif eq_val >= 0.4:
                    eq_tiers["good"].append(edge_val)
                elif eq_val >= 0.1:
                    eq_tiers["fair"].append(edge_val)
                else:
                    eq_tiers["poor"].append(edge_val)
            for tier, edges_list in eq_tiers.items():
                research_roi[tier] = {
                    "count": len(edges_list),
                    "avg_edge": round(sum(edges_list) / max(len(edges_list), 1), 2) if edges_list else 0,
                    "max_edge": round(max(edges_list, default=0), 2),
                }
        except Exception:
            pass
        stats["research_roi"] = research_roi

        # DI-F: Auto-Generated Insights — pattern-based text recommendations
        insights: list = []
        try:
            # Insight 1: Trade rate observation
            tr = stats.get("trade_rate", 0)
            if tr == 0:
                insights.append({
                    "icon": "⚠️", "type": "WARNING",
                    "title": "Zero Trade Rate",
                    "message": "No markets have passed all pipeline stages. Consider loosening risk thresholds or reviewing filter criteria.",
                })
            elif tr < 10:
                insights.append({
                    "icon": "📉", "type": "INFO",
                    "title": "Very Selective Trading",
                    "message": f"Only {tr}% of analyzed markets result in trades. The bot is being very conservative — this reduces risk but may miss opportunities.",
                })
            elif tr > 50:
                insights.append({
                    "icon": "🔥", "type": "WARNING",
                    "title": "High Trade Rate",
                    "message": f"Trading on {tr}% of analyzed markets. Consider tightening edge thresholds to improve selectivity.",
                })

            # Insight 2: Evidence quality
            avg_eq_val = stats.get("avg_eq", 0)
            if avg_eq_val == 0:
                insights.append({
                    "icon": "📚", "type": "WARNING",
                    "title": "No Research Evidence",
                    "message": "Evidence quality is 0.000 across all decisions. The web search or source fetcher may be failing. Check API keys and connectivity.",
                })
            elif avg_eq_val < 0.3:
                insights.append({
                    "icon": "📚", "type": "INFO",
                    "title": "Low Research Quality",
                    "message": f"Average evidence quality is {avg_eq_val:.3f}. Consider increasing query budgets or adding more source providers.",
                })

            # Insight 3: Grade distribution skew
            gd = stats.get("grade_distribution", {})
            f_count = gd.get("F", 0) + gd.get("D", 0)
            a_count = gd.get("A+", 0) + gd.get("A", 0)
            if f_count > total * 0.6 and total > 5:
                insights.append({
                    "icon": "🏫", "type": "WARNING",
                    "title": "Mostly Failing Grades",
                    "message": f"{f_count} of {total} decisions scored D or F. The pipeline is struggling — likely due to poor research data or misaligned edge thresholds.",
                })
            elif a_count > total * 0.3 and total > 5:
                insights.append({
                    "icon": "🌟", "type": "SUCCESS",
                    "title": "Strong Decision Quality",
                    "message": f"{a_count} of {total} decisions achieved A+/A grades. The research and forecasting pipeline is performing well.",
                })

            # Insight 4: Edge distribution
            avg_edge_val = stats.get("avg_edge", 0)
            if avg_edge_val > 15:
                insights.append({
                    "icon": "🎯", "type": "SUCCESS",
                    "title": "High Average Edge",
                    "message": f"Average edge of {avg_edge_val:.1f}% is strong. The model is finding significant mispricings.",
                })
            elif avg_edge_val < 5 and total > 5:
                insights.append({
                    "icon": "📊", "type": "INFO",
                    "title": "Low Average Edge",
                    "message": f"Average edge of {avg_edge_val:.1f}% is thin. Markets may be well-priced or the model needs calibration.",
                })

            # Insight 5: Missed opportunities
            n_missed = len(missed_opportunities)
            if n_missed >= 5:
                avg_missed_edge = sum(m["edge"] for m in missed_opportunities) / n_missed
                insights.append({
                    "icon": "💡", "type": "OPPORTUNITY",
                    "title": f"{n_missed} Missed Opportunities Detected",
                    "message": f"Found {n_missed} rejected markets with avg edge of {avg_missed_edge:.1f}%. Review risk rules — some of these may have been profitable trades.",
                })

            # Insight 6: Category concentration
            cat_dist = stats.get("category_distribution", {})
            if cat_dist:
                top_cat = max(cat_dist, key=lambda k: cat_dist[k]["total"])
                top_pct = round(cat_dist[top_cat]["total"] / max(total, 1) * 100)
                if top_pct > 60:
                    insights.append({
                        "icon": "🏷️", "type": "INFO",
                        "title": "Category Concentration",
                        "message": f"{top_pct}% of decisions are in {top_cat}. Consider diversifying market selection to reduce category-specific risk.",
                    })

            # Insight 7: Pipeline bottleneck
            funnel = stats.get("funnel", [])
            if len(funnel) >= 2:
                worst_rate = 100
                worst_stage = ""
                for f in funnel:
                    rate = f["passed"] / max(f["total"], 1) * 100
                    if rate < worst_rate and f["total"] > 0:
                        worst_rate = rate
                        worst_stage = f["stage"]
                if worst_rate < 50 and worst_stage:
                    insights.append({
                        "icon": "🔧", "type": "WARNING",
                        "title": f"Pipeline Bottleneck: {worst_stage}",
                        "message": f"Only {worst_rate:.0f}% of markets pass the {worst_stage} stage. This is the biggest drop-off point in the pipeline.",
                    })

        except Exception:
            pass
        stats["insights"] = insights

        return jsonify({"entries": entries, "cycles": cycles, "stats": stats})
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════
#  STRATEGIES & WALLETS API
# ═══════════════════════════════════════════════════════════════════

def _ensure_sw_tables(conn: Any) -> None:
    """Ensure strategies & wallets tables exist (idempotent)."""
    for tbl in ("wallets", "strategies", "strategy_wallets", "wallet_trades", "wallet_equity_snapshots"):
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        if not exists:
            from src.storage.migrations import run_migrations
            run_migrations(conn)
            break


# ─── Wallets CRUD ────────────────────────────────────────────────

@app.route("/api/wallets")
def api_wallets_list() -> Any:
    """List all wallets with stats."""
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        rows = conn.execute(
            "SELECT * FROM wallets ORDER BY created_at DESC"
        ).fetchall()
        wallets = []
        for r in rows:
            w = dict(r)
            # Attach assigned strategies
            strats = conn.execute(
                """SELECT s.id, s.name, s.strategy_type, s.icon, s.color, s.is_active,
                          sw.allocated_balance, sw.current_pnl, sw.total_trades,
                          sw.win_count, sw.loss_count, sw.is_active as binding_active
                   FROM strategy_wallets sw
                   JOIN strategies s ON s.id = sw.strategy_id
                   WHERE sw.wallet_id = ?""",
                (w["id"],),
            ).fetchall()
            w["strategies"] = [dict(s) for s in strats]
            # Count open positions from wallet_trades
            open_count = conn.execute(
                "SELECT COUNT(*) FROM wallet_trades WHERE wallet_id = ? AND status = 'open'",
                (w["id"],),
            ).fetchone()
            w["open_positions"] = open_count[0] if open_count else 0
            # Win rate
            total_t = w.get("total_trades", 0)
            w["win_rate"] = round((w.get("win_count", 0) / max(total_t, 1)) * 100, 1)
            wallets.append(w)
        return jsonify({"wallets": wallets})
    finally:
        conn.close()


@app.route("/api/wallets", methods=["POST"])
def api_wallets_create() -> Any:
    """Create a new wallet."""
    import datetime as _dt
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        data = request.get_json(force=True)
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "Wallet name is required"}), 400
        wallet_type = data.get("wallet_type", "paper")
        if wallet_type not in ("paper", "live"):
            return jsonify({"error": "wallet_type must be 'paper' or 'live'"}), 400
        address = data.get("address", "").strip()
        if wallet_type == "live" and not address:
            return jsonify({"error": "Live wallet requires an address"}), 400
        initial_balance = float(data.get("initial_balance", 10000))
        color = data.get("color", "#4c8dff")
        icon = data.get("icon", "📄" if wallet_type == "paper" else "💰")
        notes = data.get("notes", "")

        import uuid
        wallet_id = str(uuid.uuid4())[:8]
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO wallets
                (id, name, wallet_type, address, initial_balance, current_balance,
                 color, icon, notes, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (wallet_id, name, wallet_type, address, initial_balance, initial_balance,
             color, icon, notes, now, now),
        )
        conn.commit()
        return jsonify({"ok": True, "wallet_id": wallet_id})
    finally:
        conn.close()


@app.route("/api/wallets/<wallet_id>", methods=["PUT"])
def api_wallets_update(wallet_id: str) -> Any:
    """Update wallet details."""
    import datetime as _dt
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        data = request.get_json(force=True)
        fields = []
        values = []
        for key in ("name", "color", "icon", "notes", "is_active", "address"):
            if key in data:
                fields.append(f"{key} = ?")
                values.append(data[key])
        if not fields:
            return jsonify({"error": "No fields to update"}), 400
        fields.append("updated_at = ?")
        values.append(_dt.datetime.now(_dt.timezone.utc).isoformat())
        values.append(wallet_id)
        conn.execute(
            f"UPDATE wallets SET {', '.join(fields)} WHERE id = ?", values,
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/wallets/<wallet_id>", methods=["DELETE"])
def api_wallets_delete(wallet_id: str) -> Any:
    """Delete a wallet and its bindings."""
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        # Prevent deleting default wallet
        if wallet_id == "default-paper":
            return jsonify({"error": "Cannot delete the default paper wallet"}), 400
        conn.execute("DELETE FROM strategy_wallets WHERE wallet_id = ?", (wallet_id,))
        conn.execute("DELETE FROM wallet_trades WHERE wallet_id = ?", (wallet_id,))
        conn.execute("DELETE FROM wallet_equity_snapshots WHERE wallet_id = ?", (wallet_id,))
        conn.execute("DELETE FROM wallets WHERE id = ?", (wallet_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/wallets/<wallet_id>/performance")
def api_wallet_performance(wallet_id: str) -> Any:
    """Per-wallet performance analytics."""
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        w = conn.execute("SELECT * FROM wallets WHERE id = ?", (wallet_id,)).fetchone()
        if not w:
            return jsonify({"error": "Wallet not found"}), 404
        wd = dict(w)

        # Closed trades for this wallet
        trades = conn.execute(
            "SELECT * FROM wallet_trades WHERE wallet_id = ? ORDER BY opened_at DESC LIMIT 200",
            (wallet_id,),
        ).fetchall()
        trade_list = [dict(t) for t in trades]
        open_trades = [t for t in trade_list if t.get("status") == "open"]
        closed_trades = [t for t in trade_list if t.get("status") != "open"]

        # PnL stats
        total_pnl = sum(t.get("pnl", 0) for t in closed_trades)
        wins = [t for t in closed_trades if t.get("pnl", 0) > 0]
        losses = [t for t in closed_trades if t.get("pnl", 0) < 0]
        best_trade = max((t.get("pnl", 0) for t in closed_trades), default=0)
        worst_trade = min((t.get("pnl", 0) for t in closed_trades), default=0)
        avg_win = (sum(t.get("pnl", 0) for t in wins) / len(wins)) if wins else 0
        avg_loss = (sum(t.get("pnl", 0) for t in losses) / len(losses)) if losses else 0

        # Equity snapshots for this wallet
        snapshots = conn.execute(
            "SELECT * FROM wallet_equity_snapshots WHERE wallet_id = ? ORDER BY timestamp ASC LIMIT 500",
            (wallet_id,),
        ).fetchall()
        equity_curve = [dict(s) for s in snapshots]

        # Strategy assignments
        strats = conn.execute(
            """SELECT s.*, sw.allocated_balance, sw.current_pnl, sw.total_trades,
                      sw.win_count, sw.loss_count, sw.is_active as binding_active
               FROM strategy_wallets sw
               JOIN strategies s ON s.id = sw.strategy_id
               WHERE sw.wallet_id = ?""",
            (wallet_id,),
        ).fetchall()

        return jsonify({
            "wallet": wd,
            "stats": {
                "total_pnl": round(total_pnl, 2),
                "total_trades": len(closed_trades),
                "open_positions": len(open_trades),
                "win_count": len(wins),
                "loss_count": len(losses),
                "win_rate": round(len(wins) / max(len(closed_trades), 1) * 100, 1),
                "best_trade": round(best_trade, 2),
                "worst_trade": round(worst_trade, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "roi": round(total_pnl / max(wd.get("initial_balance", 1), 1) * 100, 2),
                "current_balance": wd.get("current_balance", 0),
                "initial_balance": wd.get("initial_balance", 0),
            },
            "trades": trade_list[:50],
            "equity_curve": equity_curve,
            "strategies": [dict(s) for s in strats],
        })
    finally:
        conn.close()


# ─── Strategies CRUD ─────────────────────────────────────────────

@app.route("/api/strategies")
def api_strategies_list() -> Any:
    """List all strategies with assigned wallets."""
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        rows = conn.execute(
            "SELECT * FROM strategies ORDER BY created_at DESC"
        ).fetchall()
        strategies = []
        for r in rows:
            s = dict(r)
            # Attach assigned wallets
            wallets = conn.execute(
                """SELECT w.id, w.name, w.wallet_type, w.icon, w.color,
                          w.current_balance, w.is_active,
                          sw.allocated_balance, sw.current_pnl, sw.total_trades,
                          sw.is_active as binding_active
                   FROM strategy_wallets sw
                   JOIN wallets w ON w.id = sw.wallet_id
                   WHERE sw.strategy_id = ?""",
                (s["id"],),
            ).fetchall()
            s["wallets"] = [dict(w) for w in wallets]
            strategies.append(s)
        return jsonify({"strategies": strategies})
    finally:
        conn.close()


@app.route("/api/strategies", methods=["POST"])
def api_strategies_create() -> Any:
    """Create a new strategy."""
    import datetime as _dt
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        data = request.get_json(force=True)
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "Strategy name is required"}), 400
        strategy_type = data.get("strategy_type", "ai_trading")
        valid_types = ("ai_trading", "manual", "momentum", "mean_reversion",
                       "whale_follow", "arbitrage", "custom")
        if strategy_type not in valid_types:
            return jsonify({"error": f"strategy_type must be one of: {', '.join(valid_types)}"}), 400

        description = data.get("description", "")
        risk_profile = data.get("risk_profile", "moderate")
        config_json = json.dumps(data.get("config", {}))
        icon = data.get("icon", _strategy_icon(strategy_type))
        color = data.get("color", _strategy_color(strategy_type))

        import uuid
        strategy_id = str(uuid.uuid4())[:8]
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO strategies
                (id, name, strategy_type, description, config_json,
                 risk_profile, icon, color, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (strategy_id, name, strategy_type, description, config_json,
             risk_profile, icon, color, now, now),
        )
        conn.commit()
        return jsonify({"ok": True, "strategy_id": strategy_id})
    finally:
        conn.close()


def _strategy_icon(stype: str) -> str:
    return {"ai_trading": "🤖", "manual": "🎯", "momentum": "🚀",
            "mean_reversion": "📉", "whale_follow": "🐋",
            "arbitrage": "⚖️", "custom": "🔧"}.get(stype, "📋")


def _strategy_color(stype: str) -> str:
    return {"ai_trading": "#00e68a", "manual": "#4c8dff", "momentum": "#f59e0b",
            "mean_reversion": "#a855f7", "whale_follow": "#06b6d4",
            "arbitrage": "#ec4899", "custom": "#9499b3"}.get(stype, "#4c8dff")


@app.route("/api/strategies/<strategy_id>", methods=["PUT"])
def api_strategies_update(strategy_id: str) -> Any:
    """Update a strategy."""
    import datetime as _dt
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        data = request.get_json(force=True)
        fields = []
        values = []
        for key in ("name", "description", "risk_profile", "icon", "color", "is_active"):
            if key in data:
                fields.append(f"{key} = ?")
                values.append(data[key])
        if "config" in data:
            fields.append("config_json = ?")
            values.append(json.dumps(data["config"]))
        if not fields:
            return jsonify({"error": "No fields to update"}), 400
        fields.append("updated_at = ?")
        values.append(_dt.datetime.now(_dt.timezone.utc).isoformat())
        values.append(strategy_id)
        conn.execute(
            f"UPDATE strategies SET {', '.join(fields)} WHERE id = ?", values,
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/strategies/<strategy_id>", methods=["DELETE"])
def api_strategies_delete(strategy_id: str) -> Any:
    """Delete a strategy and its bindings."""
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        if strategy_id == "default-ai":
            return jsonify({"error": "Cannot delete the default AI Trading strategy"}), 400
        conn.execute("DELETE FROM strategy_wallets WHERE strategy_id = ?", (strategy_id,))
        conn.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


# ─── Strategy ↔ Wallet Bindings ─────────────────────────────────

@app.route("/api/strategy-wallets", methods=["POST"])
def api_strategy_wallets_bind() -> Any:
    """Bind or unbind a strategy to/from a wallet."""
    import datetime as _dt
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        data = request.get_json(force=True)
        action = data.get("action", "bind")  # "bind" or "unbind"
        strategy_id = data.get("strategy_id", "")
        wallet_id = data.get("wallet_id", "")
        if not strategy_id or not wallet_id:
            return jsonify({"error": "strategy_id and wallet_id required"}), 400

        if action == "unbind":
            conn.execute(
                "DELETE FROM strategy_wallets WHERE strategy_id = ? AND wallet_id = ?",
                (strategy_id, wallet_id),
            )
            conn.commit()
            return jsonify({"ok": True, "action": "unbound"})

        allocated = float(data.get("allocated_balance", 0))
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO strategy_wallets
                (strategy_id, wallet_id, allocated_balance, is_active, created_at)
            VALUES (?,?,?,1,?)""",
            (strategy_id, wallet_id, allocated, now),
        )
        conn.commit()
        return jsonify({"ok": True, "action": "bound"})
    finally:
        conn.close()


@app.route("/api/strategy-wallets/toggle", methods=["POST"])
def api_strategy_wallets_toggle() -> Any:
    """Toggle a strategy-wallet binding active/inactive."""
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)
        data = request.get_json(force=True)
        strategy_id = data.get("strategy_id", "")
        wallet_id = data.get("wallet_id", "")
        if not strategy_id or not wallet_id:
            return jsonify({"error": "strategy_id and wallet_id required"}), 400
        row = conn.execute(
            "SELECT is_active FROM strategy_wallets WHERE strategy_id = ? AND wallet_id = ?",
            (strategy_id, wallet_id),
        ).fetchone()
        if not row:
            return jsonify({"error": "Binding not found"}), 404
        new_state = 0 if row["is_active"] else 1
        conn.execute(
            "UPDATE strategy_wallets SET is_active = ? WHERE strategy_id = ? AND wallet_id = ?",
            (new_state, strategy_id, wallet_id),
        )
        conn.commit()
        return jsonify({"ok": True, "is_active": bool(new_state)})
    finally:
        conn.close()


# ─── Strategies & Wallets Overview ───────────────────────────────

@app.route("/api/strategies-overview")
def api_strategies_overview() -> Any:
    """Combined overview of all strategies and wallets for the dashboard tab."""
    conn = _get_conn()
    try:
        _ensure_sw_tables(conn)

        # All wallets
        w_rows = conn.execute("SELECT * FROM wallets ORDER BY created_at").fetchall()
        wallets = []
        for r in w_rows:
            w = dict(r)
            strats = conn.execute(
                """SELECT s.id, s.name, s.strategy_type, s.icon, s.color, s.is_active,
                          sw.allocated_balance, sw.current_pnl, sw.total_trades,
                          sw.is_active as binding_active
                   FROM strategy_wallets sw
                   JOIN strategies s ON s.id = sw.strategy_id
                   WHERE sw.wallet_id = ?""",
                (w["id"],),
            ).fetchall()
            w["strategies"] = [dict(s) for s in strats]
            # Open position count
            open_ct = conn.execute(
                "SELECT COUNT(*) FROM wallet_trades WHERE wallet_id = ? AND status = 'open'",
                (w["id"],),
            ).fetchone()
            w["open_positions"] = open_ct[0] if open_ct else 0
            w["win_rate"] = round((w.get("win_count", 0) / max(w.get("total_trades", 1), 1)) * 100, 1)
            wallets.append(w)

        # All strategies
        s_rows = conn.execute("SELECT * FROM strategies ORDER BY created_at").fetchall()
        strategies = []
        for r in s_rows:
            s = dict(r)
            s_wallets = conn.execute(
                """SELECT w.id, w.name, w.wallet_type, w.icon, w.color,
                          w.current_balance, w.is_active,
                          sw.allocated_balance, sw.current_pnl, sw.total_trades,
                          sw.is_active as binding_active
                   FROM strategy_wallets sw
                   JOIN wallets w ON w.id = sw.wallet_id
                   WHERE sw.strategy_id = ?""",
                (s["id"],),
            ).fetchall()
            s["wallets"] = [dict(w) for w in s_wallets]
            strategies.append(s)

        # Summary stats
        total_balance = sum(w.get("current_balance", 0) for w in wallets)
        total_pnl = sum(w.get("total_pnl", 0) for w in wallets)
        total_trades = sum(w.get("total_trades", 0) for w in wallets)
        paper_count = sum(1 for w in wallets if w.get("wallet_type") == "paper")
        live_count = sum(1 for w in wallets if w.get("wallet_type") == "live")
        active_strats = sum(1 for s in strategies if s.get("is_active"))

        return jsonify({
            "wallets": wallets,
            "strategies": strategies,
            "summary": {
                "total_wallets": len(wallets),
                "paper_wallets": paper_count,
                "live_wallets": live_count,
                "total_strategies": len(strategies),
                "active_strategies": active_strats,
                "total_balance": round(total_balance, 2),
                "total_pnl": round(total_pnl, 2),
                "total_trades": total_trades,
            },
        })
    finally:
        conn.close()


# ─── API: Audit Trail ───────────────────────────────────────────

@app.route("/api/audit")
def api_audit() -> Any:
    conn = _get_conn()
    try:
        # Check if audit_trail table exists
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_trail'"
        ).fetchone()
        if not tables:
            return jsonify({"entries": [], "summary": {"total_entries": 0}})

        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY timestamp DESC LIMIT 50"
        ).fetchall()
        entries = [dict(r) for r in rows]
        return jsonify({
            "entries": entries,
            "summary": {"total_entries": len(entries)},
        })
    finally:
        conn.close()


# ─── API: Kill Switch Toggle ────────────────────────────────────

@app.route("/api/kill-switch", methods=["POST"])
def api_kill_switch() -> Any:
    """Toggle kill switch (for dashboard button)."""
    cfg = _get_config()
    # In production this would persist to config file
    current = cfg.risk.kill_switch
    # Note: this only affects the in-memory config
    cfg.risk.kill_switch = not current
    return jsonify({
        "kill_switch": cfg.risk.kill_switch,
        "message": f"Kill switch {'ENGAGED' if cfg.risk.kill_switch else 'DISENGAGED'}",
    })


# ─── API: Execution Quality ─────────────────────────────────────

@app.route("/api/execution-quality")
def api_execution_quality() -> Any:
    conn = _get_conn()
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fill_records'"
        ).fetchone()
        if not tables:
            return jsonify({
                "total_orders": 0,
                "avg_fill_rate": 0,
                "avg_slippage_bps": 0,
                "strategy_stats": {},
            })

        rows = conn.execute(
            "SELECT * FROM fill_records ORDER BY timestamp DESC LIMIT 100"
        ).fetchall()
        fills = [dict(r) for r in rows]
        return jsonify({
            "total_orders": len(fills),
            "fills": fills[:20],
        })
    finally:
        conn.close()


# ─── API: Whale / Wallet Scanner ─────────────────────────────────

@app.route("/api/whale-activity")
def api_whale_activity() -> Any:
    """Return comprehensive whale tracker data with analytics."""
    conn = _get_conn()
    try:
        # Check if tables exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tracked_wallets'"
        ).fetchone()
        if not tables:
            return jsonify(_whale_empty_response())

        # ── Core Data ─────────────────────────────────────────
        # Tracked wallets (sorted by score)
        w_rows = conn.execute(
            "SELECT * FROM tracked_wallets ORDER BY score DESC"
        ).fetchall()
        wallets = [dict(r) for r in w_rows]

        # Conviction signals (latest per market)
        s_rows = conn.execute(
            "SELECT * FROM wallet_signals ORDER BY conviction_score DESC LIMIT 50"
        ).fetchall()
        signals = []
        for r in s_rows:
            d = dict(r)
            try:
                d["whale_names"] = json.loads(d.get("whale_names_json", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["whale_names"] = []
            signals.append(d)

        # Recent deltas (last 200)
        d_rows = conn.execute(
            "SELECT * FROM wallet_deltas ORDER BY detected_at DESC LIMIT 200"
        ).fetchall()
        deltas = [dict(r) for r in d_rows]

        # Starred items
        starred_rows = conn.execute("SELECT star_type, identifier FROM whale_stars").fetchall()
        starred_whales = set()
        starred_markets = set()
        for sr in starred_rows:
            srd = dict(sr)
            if srd["star_type"] == "whale":
                starred_whales.add(srd["identifier"])
            elif srd["star_type"] == "market":
                starred_markets.add(srd["identifier"])
        # Annotate wallets and signals with star status
        for w in wallets:
            w["is_starred"] = w.get("address", "") in starred_whales
        for s in signals:
            s["is_starred"] = s.get("market_slug", "") in starred_markets

        # ── Derived Analytics ─────────────────────────────────

        # 1. Summary stats
        strong_count = sum(1 for s in signals if s.get("signal_strength") == "STRONG")
        moderate_count = sum(1 for s in signals if s.get("signal_strength") == "MODERATE")
        weak_count = sum(1 for s in signals if s.get("signal_strength") == "WEAK")
        new_entries = sum(1 for d in deltas if d.get("action") == "NEW_ENTRY")
        size_increases = sum(1 for d in deltas if d.get("action") == "SIZE_INCREASE")
        size_decreases = sum(1 for d in deltas if d.get("action") == "SIZE_DECREASE")
        exits = sum(1 for d in deltas if d.get("action") == "EXIT")
        last_scan = wallets[0].get("last_scanned") if wallets else None

        # 2. Smart Money Index (0-100)
        # Weighted average of all signal conviction scores, biased by direction
        bullish_weight = sum(
            s.get("conviction_score", 0) * s.get("total_whale_usd", 0)
            for s in signals if s.get("direction") == "BULLISH"
        )
        bearish_weight = sum(
            s.get("conviction_score", 0) * s.get("total_whale_usd", 0)
            for s in signals if s.get("direction") == "BEARISH"
        )
        total_weight = bullish_weight + bearish_weight
        # SMI: 50 = neutral, >50 = bullish, <50 = bearish
        smart_money_index = 50.0
        if total_weight > 0:
            smart_money_index = (bullish_weight / total_weight) * 100

        # 3. Net whale flow ($ entering vs exiting)
        flow_in = sum(d.get("value_change_usd", 0) for d in deltas
                       if d.get("action") in ("NEW_ENTRY", "SIZE_INCREASE"))
        flow_out = abs(sum(d.get("value_change_usd", 0) for d in deltas
                           if d.get("action") in ("EXIT", "SIZE_DECREASE")))
        net_flow = flow_in - flow_out

        # 4. Aggregate whale stats
        total_whale_pnl = sum(w.get("total_pnl", 0) for w in wallets)
        avg_win_rate = (sum(w.get("win_rate", 0) for w in wallets) / len(wallets)) if wallets else 0
        total_positions = sum(w.get("active_positions", 0) for w in wallets)
        total_volume = sum(w.get("total_volume", 0) for w in wallets)

        # 5. Top conviction market
        top_conviction = signals[0] if signals else None

        # 6. Action breakdown for flow chart
        action_breakdown = {
            "new_entries": new_entries,
            "size_increases": size_increases,
            "size_decreases": size_decreases,
            "exits": exits,
        }

        # 7. Market concentration — which markets have most whale $
        market_concentration: dict = {}
        for s in signals:
            slug = s.get("market_slug", "unknown")
            title = s.get("title", slug)[:60]
            if slug not in market_concentration:
                market_concentration[slug] = {
                    "market_slug": slug,
                    "title": title,
                    "total_usd": 0,
                    "whale_count": 0,
                    "direction": s.get("direction", ""),
                    "conviction": s.get("conviction_score", 0),
                }
            market_concentration[slug]["total_usd"] += s.get("total_whale_usd", 0)
            market_concentration[slug]["whale_count"] = max(
                market_concentration[slug]["whale_count"], s.get("whale_count", 0)
            )
        top_markets = sorted(
            market_concentration.values(),
            key=lambda x: x["total_usd"],
            reverse=True,
        )[:10]

        # 8. Per-whale activity count (for leaderboard enrichment)
        whale_activity_counts: dict = {}
        for d in deltas:
            name = d.get("wallet_name", "")
            if name not in whale_activity_counts:
                whale_activity_counts[name] = {"entries": 0, "exits": 0, "total": 0}
            whale_activity_counts[name]["total"] += 1
            if d.get("action") == "NEW_ENTRY":
                whale_activity_counts[name]["entries"] += 1
            elif d.get("action") == "EXIT":
                whale_activity_counts[name]["exits"] += 1

        # Enrich wallets with activity counts
        for w in wallets:
            name = w.get("name", "")
            ac = whale_activity_counts.get(name, {})
            w["recent_entries"] = ac.get("entries", 0)
            w["recent_exits"] = ac.get("exits", 0)
            w["recent_activity"] = ac.get("total", 0)
            # PnL tier for visual grouping
            pnl = w.get("total_pnl", 0)
            if pnl >= 2_000_000:
                w["tier"] = "LEGENDARY"
            elif pnl >= 1_000_000:
                w["tier"] = "ELITE"
            elif pnl >= 500_000:
                w["tier"] = "PRO"
            else:
                w["tier"] = "RISING"

        # 9. Signal direction distribution
        direction_dist = {
            "bullish": sum(1 for s in signals if s.get("direction") == "BULLISH"),
            "bearish": sum(1 for s in signals if s.get("direction") == "BEARISH"),
        }

        # 10. Whale overlap — markets where 3+ whales agree
        high_consensus = [s for s in signals if (s.get("whale_count", 0) >= 3)]

        # 11. Market category detection
        _CATEGORY_RULES = [
            ("NBA", ["nba-", "nba ", "celtics", "lakers", "knicks", "warriors",
                      "pistons", "76ers", "bucks", "heat", "rockets", "nuggets",
                      "cavaliers", "timberwolves", "hawks", "thunder", "spurs",
                      "mavericks", "suns", "grizzlies", "raptors", "pacers",
                      "clippers", "magic", "bulls"]),
            ("NFL", ["nfl-", "nfl ", "super bowl", "chiefs", "eagles", "texans",
                      "ravens", "49ers", "bears", "rams", "raiders", "colts",
                      "giants"]),
            ("Soccer", ["epl-", "ucl-", "cdr-", "lal-", "spl-", "bra-", "uef-",
                         "arsenal", "barcelona", "bayern", "chelsea", "liverpool",
                         "sevilla", "madrid", "napoli", "psg", "brentford",
                         "bournemouth", "nottingham", "everton", "newcastle"]),
            ("Politics", ["trump", "biden", "president", "republican", "democrat",
                          "election", "congress", "senate", "governor"]),
            ("Olympics", ["olympic", "winter games", "gold medal", "ice hockey"]),
            ("MLB", ["mlb-", "world series", "yankees", "phillies", "orioles"]),
            ("Golf", ["masters", "pga", "scheffler", "open championship"]),
            ("Crypto", ["bitcoin", "ethereum", "btc", "eth", "crypto"]),
        ]

        def _detect_category(slug: str, title: str) -> str:
            key = (slug + " " + title).lower()
            for cat, keywords in _CATEGORY_RULES:
                if any(kw in key for kw in keywords):
                    return cat
            return "Other"

        # Enrich signals with category + freshness
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        for s in signals:
            s["category"] = _detect_category(
                s.get("market_slug", ""), s.get("title", "")
            )
            # Signal freshness
            detected = s.get("detected_at")
            if detected:
                try:
                    dt = datetime.fromisoformat(detected.replace("Z", "+00:00"))
                    age_hours = (now_utc - dt).total_seconds() / 3600
                    s["age_hours"] = round(age_hours, 1)
                    if age_hours < 1:
                        s["freshness"] = "LIVE"
                    elif age_hours < 6:
                        s["freshness"] = "FRESH"
                    elif age_hours < 24:
                        s["freshness"] = "RECENT"
                    elif age_hours < 72:
                        s["freshness"] = "AGING"
                    else:
                        s["freshness"] = "STALE"
                except (ValueError, TypeError):
                    s["age_hours"] = None
                    s["freshness"] = "UNKNOWN"
            else:
                s["age_hours"] = None
                s["freshness"] = "UNKNOWN"

            # Price edge: how far current price is from whale avg entry
            avg_p = s.get("avg_whale_price") or 0
            cur_p = s.get("current_price") or 0
            if avg_p > 0 and cur_p > 0:
                edge = ((cur_p - avg_p) / avg_p) * 100
                s["price_edge_pct"] = round(edge, 1)
            else:
                s["price_edge_pct"] = None

        # 12. Category breakdown
        category_counts: dict = {}
        category_usd: dict = {}
        for s in signals:
            cat = s.get("category", "Other")
            category_counts[cat] = category_counts.get(cat, 0) + 1
            category_usd[cat] = category_usd.get(cat, 0) + (
                s.get("total_whale_usd", 0)
            )
        categories_summary = [
            {
                "category": cat,
                "count": category_counts[cat],
                "total_usd": round(category_usd.get(cat, 0), 2),
            }
            for cat in sorted(category_counts, key=category_counts.get, reverse=True)  # type: ignore[arg-type]
        ]

        # 13. Per-whale portfolio breakdown
        whale_portfolios: dict = {}
        for s in signals:
            for wn in s.get("whale_names", []):
                if wn not in whale_portfolios:
                    whale_portfolios[wn] = {}
                cat = s.get("category", "Other")
                whale_portfolios[wn][cat] = whale_portfolios[wn].get(cat, 0) + (
                    s.get("total_whale_usd", 0)
                )

        # 14. Activity timeline — bucket deltas by hour (last 24h)
        activity_timeline: list = []
        for i in range(24):
            hour_label = f"{i}h ago" if i > 0 else "Now"
            count = 0
            usd = 0.0
            for d in deltas:
                det = d.get("detected_at")
                if not det:
                    continue
                try:
                    dt = datetime.fromisoformat(det.replace("Z", "+00:00"))
                    hours_ago = (now_utc - dt).total_seconds() / 3600
                    if i <= hours_ago < i + 1:
                        count += 1
                        usd += abs(d.get("value_change_usd", 0))
                except (ValueError, TypeError):
                    pass
            activity_timeline.append({
                "label": hour_label, "count": count,
                "usd": round(usd, 2),
            })

        # 15. Signal strength distribution for summary
        strength_dist = {
            "STRONG": strong_count,
            "MODERATE": moderate_count,
            "WEAK": weak_count,
        }

        # 16. Freshness distribution
        freshness_counts: dict = {}
        for s in signals:
            f = s.get("freshness", "UNKNOWN")
            freshness_counts[f] = freshness_counts.get(f, 0) + 1

        # 17. Momentum — net flow in windows (1h, 6h, 12h, 24h)
        def _window_flow(hours: float) -> dict:
            f_in = 0.0
            f_out = 0.0
            count_in = 0
            count_out = 0
            for d in deltas:
                det = d.get("detected_at")
                if not det:
                    continue
                try:
                    ddt = datetime.fromisoformat(det.replace("Z", "+00:00"))
                    age = (now_utc - ddt).total_seconds() / 3600
                    if age <= hours:
                        val = d.get("value_change_usd", 0)
                        if d.get("action") in ("NEW_ENTRY", "SIZE_INCREASE"):
                            f_in += val
                            count_in += 1
                        elif d.get("action") in ("EXIT", "SIZE_DECREASE"):
                            f_out += abs(val)
                            count_out += 1
                except (ValueError, TypeError):
                    pass
            net = f_in - f_out
            return {
                "window": f"{int(hours)}h",
                "flow_in": round(f_in, 2),
                "flow_out": round(f_out, 2),
                "net_flow": round(net, 2),
                "count_in": count_in,
                "count_out": count_out,
                "direction": "ACCUMULATING" if net > 0 else "DISTRIBUTING" if net < 0 else "NEUTRAL",
            }

        momentum = [_window_flow(h) for h in (1, 6, 12, 24)]

        # 18. Whale-Market overlap matrix — which whales share markets
        whale_market_map: dict = {}   # whale_name -> set of market_slugs
        for s in signals:
            for wn in s.get("whale_names", []):
                if wn not in whale_market_map:
                    whale_market_map[wn] = set()
                whale_market_map[wn].add(s.get("market_slug", ""))

        # Build overlap counts: how many markets two whales share
        whale_names_list = sorted(whale_market_map.keys())
        overlap_matrix: list = []
        for i, w1 in enumerate(whale_names_list):
            for w2 in whale_names_list[i + 1:]:
                shared = whale_market_map[w1] & whale_market_map[w2]
                if shared:
                    overlap_matrix.append({
                        "whale_a": w1,
                        "whale_b": w2,
                        "shared_markets": len(shared),
                        "market_names": [
                            (s.get("title", s.get("market_slug", ""))[:40])
                            for s in signals
                            if s.get("market_slug") in shared
                        ][:5],
                    })
        overlap_matrix.sort(key=lambda x: x["shared_markets"], reverse=True)

        # Herd Behavior Index: avg overlap across whales (0-100)
        if len(whale_names_list) > 1:
            total_possible_pairs = len(whale_names_list) * (len(whale_names_list) - 1) / 2
            total_overlap = sum(o["shared_markets"] for o in overlap_matrix)
            # Normalize: each pair could share up to max(markets per whale) markets
            max_markets = max((len(v) for v in whale_market_map.values()), default=1)
            herd_index = min(100.0, (total_overlap / (total_possible_pairs * max(max_markets, 1))) * 100)
        else:
            herd_index = 0.0

        # 19. Per-market accumulation/distribution
        market_accum: dict = {}
        for d in deltas:
            slug = d.get("market_slug", "")
            title = d.get("title", slug)[:50]
            if slug not in market_accum:
                market_accum[slug] = {"title": title, "buys": 0.0, "sells": 0.0, "buy_count": 0, "sell_count": 0}
            val = abs(d.get("value_change_usd", 0))
            if d.get("action") in ("NEW_ENTRY", "SIZE_INCREASE"):
                market_accum[slug]["buys"] += val
                market_accum[slug]["buy_count"] += 1
            elif d.get("action") in ("EXIT", "SIZE_DECREASE"):
                market_accum[slug]["sells"] += val
                market_accum[slug]["sell_count"] += 1
        # Calculate net and ratio
        accum_dist: list = []
        for slug, ad in market_accum.items():
            total = ad["buys"] + ad["sells"]
            if total < 100:
                continue  # skip tiny activity
            net = ad["buys"] - ad["sells"]
            ratio = ad["buys"] / max(ad["sells"], 1)
            accum_dist.append({
                "market_slug": slug,
                "title": ad["title"],
                "buys_usd": round(ad["buys"], 2),
                "sells_usd": round(ad["sells"], 2),
                "net_usd": round(net, 2),
                "ratio": round(ratio, 2),
                "signal": "STRONG_ACCUM" if ratio > 3 else "ACCUM" if ratio > 1.5 else "NEUTRAL" if ratio > 0.67 else "DISTRIB" if ratio > 0.33 else "STRONG_DISTRIB",
                "buy_count": ad["buy_count"],
                "sell_count": ad["sell_count"],
            })
        accum_dist.sort(key=lambda x: abs(x["net_usd"]), reverse=True)

        # 20. Position velocity (actions/hour in recent windows)
        def _velocity(hours: float) -> float:
            cnt = 0
            for d in deltas:
                det = d.get("detected_at")
                if not det:
                    continue
                try:
                    ddt = datetime.fromisoformat(det.replace("Z", "+00:00"))
                    if (now_utc - ddt).total_seconds() / 3600 <= hours:
                        cnt += 1
                except (ValueError, TypeError):
                    pass
            return round(cnt / max(hours, 1), 2)

        velocity = {
            "1h": _velocity(1),
            "6h": _velocity(6),
            "24h": _velocity(24),
        }

        # 21. Auto-generated risk alerts
        def _safe_age_hours(ts_str, ref_utc):
            if not ts_str:
                return None
            try:
                ddt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                return (ref_utc - ddt).total_seconds() / 3600
            except (ValueError, TypeError):
                return None

        risk_alerts: list = []
        # Alert: mass exit (>5 exits in last 6h)
        exit_6h = sum(
            1 for d in deltas
            if d.get("action") == "EXIT" and d.get("detected_at")
            and _safe_age_hours(d.get("detected_at"), now_utc) is not None
            and _safe_age_hours(d.get("detected_at"), now_utc) <= 6  # type: ignore[operator]
        )
        if exit_6h >= 5:
            risk_alerts.append({
                "level": "HIGH",
                "type": "MASS_EXIT",
                "message": f"⚠️ {exit_6h} whale exits in last 6h — potential smart money distribution",
                "count": exit_6h,
            })
        elif exit_6h >= 3:
            risk_alerts.append({
                "level": "MEDIUM",
                "type": "ELEVATED_EXITS",
                "message": f"🔶 {exit_6h} exits in 6h — elevated exit activity",
                "count": exit_6h,
            })
        # Alert: extreme SMI
        if smart_money_index >= 80:
            risk_alerts.append({
                "level": "MEDIUM",
                "type": "EXTREME_BULLISH",
                "message": f"🟢 SMI at {smart_money_index:.0f} — extreme bullish conviction, potential crowding",
            })
        elif smart_money_index <= 20:
            risk_alerts.append({
                "level": "HIGH",
                "type": "EXTREME_BEARISH",
                "message": f"🔴 SMI at {smart_money_index:.0f} — extreme bearish conviction",
            })
        # Alert: high concentration (single market >40% of whale capital)
        total_signal_usd = sum(s.get("total_whale_usd", 0) for s in signals)
        if total_signal_usd > 0:
            for tm in top_markets[:3]:
                pct = (tm["total_usd"] / total_signal_usd) * 100
                if pct > 40:
                    risk_alerts.append({
                        "level": "MEDIUM",
                        "type": "HIGH_CONCENTRATION",
                        "message": f"📊 {pct:.0f}% of whale capital concentrated in '{tm['title'][:35]}'",
                    })
        # Alert: herd behavior
        if herd_index > 50:
            risk_alerts.append({
                "level": "MEDIUM" if herd_index <= 70 else "HIGH",
                "type": "HERD_BEHAVIOR",
                "message": f"🐑 Herd index at {herd_index:.0f}% — whales are highly correlated, watch for cascade risk",
            })
        # Alert: velocity spike
        if velocity["1h"] >= 5:
            risk_alerts.append({
                "level": "HIGH",
                "type": "VELOCITY_SPIKE",
                "message": f"⚡ Position velocity at {velocity['1h']}/hr — unusual activity spike",
            })
        # Alert: stale data
        if last_scan:
            try:
                scan_dt = datetime.fromisoformat(last_scan.replace("Z", "+00:00"))
                scan_age_h = (now_utc - scan_dt).total_seconds() / 3600
                if scan_age_h > 2:
                    risk_alerts.append({
                        "level": "LOW",
                        "type": "STALE_DATA",
                        "message": f"🕐 Last scan was {scan_age_h:.1f}h ago — data may be stale",
                    })
            except (ValueError, TypeError):
                pass

        # 22. Whale performance tiers summary
        tier_summary: dict = {}
        for w in wallets:
            tier = w.get("tier", "RISING")
            if tier not in tier_summary:
                tier_summary[tier] = {"count": 0, "total_pnl": 0.0, "avg_winrate": 0.0, "total_volume": 0.0}
            tier_summary[tier]["count"] += 1
            tier_summary[tier]["total_pnl"] += w.get("total_pnl", 0)
            tier_summary[tier]["total_volume"] += w.get("total_volume", 0)
        for tier_data in tier_summary.values():
            if tier_data["count"] > 0:
                tier_wallets = [w for w in wallets if w.get("tier") == tier]
                tier_data["avg_winrate"] = round(
                    sum(w.get("win_rate", 0) for w in tier_wallets) / len(tier_wallets), 4
                ) if tier_wallets else 0

        # ── Enhanced Whale Analytics (23–27) ─────────────────────

        # 23. Persistent alert history — store risk_alerts and return last 50
        alert_history: list = []
        try:
            for ra in risk_alerts:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO whale_alert_history
                           (alert_type, level, message, detail_json)
                           VALUES (?, ?, ?, ?)""",
                        (ra.get("type", ""), ra.get("level", ""),
                         ra.get("message", ""), json.dumps(ra)),
                    )
                except Exception:
                    pass
            conn.commit()
            ah_rows = conn.execute(
                "SELECT * FROM whale_alert_history ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            for ah in ah_rows:
                ahd = dict(ah)
                alert_history.append({
                    "id": ahd.get("id"),
                    "alert_type": ahd.get("alert_type", ""),
                    "level": ahd.get("level", ""),
                    "message": ahd.get("message", ""),
                    "created_at": ahd.get("created_at", ""),
                })
        except Exception:
            pass

        # 24. Conviction trend tracking — compare current vs previous snapshot
        try:
            # Build a lookup of previous snapshots (latest per market_slug)
            prev_rows = conn.execute(
                """SELECT market_slug, conviction_score, whale_count, total_whale_usd,
                          snapped_at
                   FROM conviction_snapshots
                   WHERE id IN (
                       SELECT MAX(id) FROM conviction_snapshots
                       GROUP BY market_slug
                   )"""
            ).fetchall()
            prev_map: dict = {}
            for pr in prev_rows:
                prd = dict(pr)
                prev_map[prd["market_slug"]] = prd

            # Enrich each signal with trend data
            for s in signals:
                slug = s.get("market_slug", "")
                cur_conv = s.get("conviction_score", 0) or 0
                cur_wc = s.get("whale_count", 0) or 0
                cur_usd = s.get("total_whale_usd", 0) or 0
                prev = prev_map.get(slug)
                if prev:
                    prev_conv = prev.get("conviction_score", 0) or 0
                    delta = cur_conv - prev_conv
                    s["conviction_delta"] = round(delta, 2)
                    s["whale_count_delta"] = cur_wc - (prev.get("whale_count", 0) or 0)
                    s["usd_delta"] = round(cur_usd - (prev.get("total_whale_usd", 0) or 0), 2)
                    if delta > 2:
                        s["trend"] = "RISING"
                    elif delta < -2:
                        s["trend"] = "FALLING"
                    else:
                        s["trend"] = "STABLE"
                else:
                    s["trend"] = "NEW"
                    s["conviction_delta"] = 0
                    s["whale_count_delta"] = 0
                    s["usd_delta"] = 0

            # Save new snapshots for next comparison
            for s in signals:
                try:
                    conn.execute(
                        """INSERT INTO conviction_snapshots
                           (market_slug, outcome, conviction_score, whale_count, total_whale_usd)
                           VALUES (?, ?, ?, ?, ?)""",
                        (s.get("market_slug", ""),
                         s.get("outcome", ""),
                         s.get("conviction_score", 0),
                         s.get("whale_count", 0),
                         s.get("total_whale_usd", 0)),
                    )
                except Exception:
                    pass
            conn.commit()
        except Exception:
            # If anything fails, fill defaults
            for s in signals:
                s.setdefault("trend", "NEW")
                s.setdefault("conviction_delta", 0)
                s.setdefault("whale_count_delta", 0)
                s.setdefault("usd_delta", 0)

        # 25. Copy-trade simulation per whale
        # For each whale: sum PnL of signals they participated in, win rate
        try:
            for w in wallets:
                wname = w.get("name", "")
                whale_signals = [
                    s for s in signals if wname in s.get("whale_names", [])
                ]
                if whale_signals:
                    # Simulate: if you entered at avg_whale_price, current = current_price
                    follow_pnl = 0.0
                    follow_wins = 0
                    follow_total = 0
                    for ws in whale_signals:
                        avg_p = ws.get("avg_whale_price") or 0
                        cur_p = ws.get("current_price") or 0
                        direction = ws.get("direction", "")
                        if avg_p > 0 and cur_p > 0:
                            follow_total += 1
                            if direction == "BULLISH":
                                pnl_pct = (cur_p - avg_p) / avg_p
                            else:
                                pnl_pct = (avg_p - cur_p) / avg_p
                            follow_pnl += pnl_pct * 100
                            if pnl_pct > 0:
                                follow_wins += 1
                    w["follow_pnl"] = round(follow_pnl, 2)
                    w["follow_trades"] = follow_total
                    w["follow_win_rate"] = round(
                        follow_wins / max(follow_total, 1) * 100, 1
                    )
                    wr = w["follow_win_rate"]
                    if wr >= 65 and follow_total >= 3:
                        w["follow_signal"] = "STRONG_FOLLOW"
                    elif wr >= 50:
                        w["follow_signal"] = "FOLLOW"
                    elif wr >= 35:
                        w["follow_signal"] = "CAUTION"
                    else:
                        w["follow_signal"] = "AVOID"
                else:
                    w["follow_pnl"] = 0
                    w["follow_trades"] = 0
                    w["follow_win_rate"] = 0
                    w["follow_signal"] = "NO_DATA"
        except Exception:
            for w in wallets:
                w.setdefault("follow_pnl", 0)
                w.setdefault("follow_trades", 0)
                w.setdefault("follow_win_rate", 0)
                w.setdefault("follow_signal", "NO_DATA")

        # 26. Holding duration analytics per whale
        try:
            for w in wallets:
                wname = w.get("name", "")
                # Find entry → exit pairs in deltas for this whale
                entries: dict = {}  # market_slug → entry detected_at
                hold_durations: list = []
                wdeltas = [d for d in deltas if d.get("wallet_name", "") == wname]
                wdeltas.sort(key=lambda x: x.get("detected_at", ""))
                for d in wdeltas:
                    slug = d.get("market_slug", "")
                    action = d.get("action", "")
                    det = d.get("detected_at")
                    if action == "NEW_ENTRY" and det:
                        entries[slug] = det
                    elif action == "EXIT" and det and slug in entries:
                        try:
                            entry_dt = datetime.fromisoformat(
                                entries[slug].replace("Z", "+00:00")
                            )
                            exit_dt = datetime.fromisoformat(
                                det.replace("Z", "+00:00")
                            )
                            hrs = (exit_dt - entry_dt).total_seconds() / 3600
                            if hrs > 0:
                                hold_durations.append(hrs)
                        except (ValueError, TypeError):
                            pass
                        entries.pop(slug, None)
                if hold_durations:
                    avg_hold = sum(hold_durations) / len(hold_durations)
                    w["avg_hold_hours"] = round(avg_hold, 1)
                    w["hold_count"] = len(hold_durations)
                    if avg_hold < 6:
                        w["hold_style"] = "SCALPER"
                    elif avg_hold < 48:
                        w["hold_style"] = "SWING"
                    elif avg_hold < 168:
                        w["hold_style"] = "POSITION"
                    else:
                        w["hold_style"] = "HODLER"
                else:
                    w["avg_hold_hours"] = None
                    w["hold_count"] = 0
                    w["hold_style"] = "UNKNOWN"
        except Exception:
            for w in wallets:
                w.setdefault("avg_hold_hours", None)
                w.setdefault("hold_count", 0)
                w.setdefault("hold_style", "UNKNOWN")

        # Add signal_age_hours to each signal (time since detected_at)
        for s in signals:
            det = s.get("detected_at")
            if det:
                try:
                    dt = datetime.fromisoformat(det.replace("Z", "+00:00"))
                    s["signal_age_hours"] = round(
                        (now_utc - dt).total_seconds() / 3600, 1
                    )
                except (ValueError, TypeError):
                    s["signal_age_hours"] = None
            else:
                s["signal_age_hours"] = None

        # 27. Whale correlation scores — Jaccard similarity + direction agreement
        try:
            for ov in overlap_matrix:
                w_a = ov["whale_a"]
                w_b = ov["whale_b"]
                set_a = whale_market_map.get(w_a, set())
                set_b = whale_market_map.get(w_b, set())
                union_size = len(set_a | set_b)
                inter_size = len(set_a & set_b)
                # Jaccard similarity as percentage
                ov["correlation_pct"] = round(
                    (inter_size / max(union_size, 1)) * 100, 1
                )
                # Direction agreement: for shared markets, do they agree?
                shared = set_a & set_b
                agree = 0
                disagree = 0
                for slug in shared:
                    # Find signals for each whale in this market
                    a_dir = None
                    b_dir = None
                    for s in signals:
                        if s.get("market_slug") != slug:
                            continue
                        wnames = s.get("whale_names", [])
                        if w_a in wnames and a_dir is None:
                            a_dir = s.get("direction")
                        if w_b in wnames and b_dir is None:
                            b_dir = s.get("direction")
                    if a_dir and b_dir:
                        if a_dir == b_dir:
                            agree += 1
                        else:
                            disagree += 1
                total_compared = agree + disagree
                ov["agree_count"] = agree
                ov["disagree_count"] = disagree
                ov["agreement_rate"] = round(
                    (agree / max(total_compared, 1)) * 100, 1
                )
                corr_pct = ov["correlation_pct"]
                if corr_pct >= 50:
                    ov["correlation_label"] = "HIGH"
                elif corr_pct >= 25:
                    ov["correlation_label"] = "MODERATE"
                else:
                    ov["correlation_label"] = "LOW"
        except Exception:
            for ov in overlap_matrix:
                ov.setdefault("correlation_pct", 0)
                ov.setdefault("agree_count", 0)
                ov.setdefault("disagree_count", 0)
                ov.setdefault("agreement_rate", 0)
                ov.setdefault("correlation_label", "LOW")

        return jsonify({
            "tracked_wallets": wallets,
            "conviction_signals": signals,
            "recent_deltas": deltas,
            "high_consensus_signals": [
                {
                    "title": s.get("title", "")[:60],
                    "whale_count": s.get("whale_count", 0),
                    "total_usd": round(s.get("total_whale_usd", 0), 2),
                    "conviction": round(s.get("conviction_score", 0), 1),
                    "direction": s.get("direction", ""),
                    "whale_names": s.get("whale_names", []),
                }
                for s in high_consensus
            ],
            "top_markets": top_markets,
            "categories": categories_summary,
            "whale_portfolios": whale_portfolios,
            "activity_timeline": activity_timeline,
            "momentum": momentum,
            "whale_overlap": overlap_matrix[:20],
            "herd_index": round(herd_index, 1),
            "accumulation_distribution": accum_dist[:15],
            "velocity": velocity,
            "risk_alerts": risk_alerts,
            "tier_summary": tier_summary,
            "alert_history": alert_history,
            "summary": {
                "total_wallets": len(wallets),
                "total_signals": len(signals),
                "strong_signals": strong_count,
                "moderate_signals": moderate_count,
                "weak_signals": weak_count,
                "recent_entries": new_entries,
                "recent_exits": exits,
                "last_scan": last_scan,
                "smart_money_index": round(smart_money_index, 1),
                "net_flow": round(net_flow, 2),
                "flow_in": round(flow_in, 2),
                "flow_out": round(flow_out, 2),
                "total_whale_pnl": round(total_whale_pnl, 2),
                "avg_win_rate": round(avg_win_rate, 4),
                "total_positions": total_positions,
                "total_volume": round(total_volume, 2),
                "action_breakdown": action_breakdown,
                "direction_distribution": direction_dist,
                "strength_distribution": strength_dist,
                "freshness_distribution": freshness_counts,
                "top_conviction_market": (
                    top_conviction.get("title", "")[:50] if top_conviction else "—"
                ),
                "top_conviction_score": (
                    round(top_conviction.get("conviction_score", 0), 1) if top_conviction else 0
                ),
            },
        })
    finally:
        conn.close()


def _whale_empty_response() -> dict:
    """Return empty whale data structure."""
    return {
        "tracked_wallets": [],
        "conviction_signals": [],
        "recent_deltas": [],
        "high_consensus_signals": [],
        "top_markets": [],
        "categories": [],
        "whale_portfolios": {},
        "activity_timeline": [],
        "momentum": [],
        "whale_overlap": [],
        "herd_index": 0.0,
        "accumulation_distribution": [],
        "velocity": {"1h": 0, "6h": 0, "24h": 0},
        "risk_alerts": [],
        "tier_summary": {},
        "alert_history": [],
        "summary": {
            "total_wallets": 0,
            "total_signals": 0,
            "strong_signals": 0,
            "moderate_signals": 0,
            "weak_signals": 0,
            "recent_entries": 0,
            "recent_exits": 0,
            "last_scan": None,
            "smart_money_index": 50.0,
            "net_flow": 0.0,
            "flow_in": 0.0,
            "flow_out": 0.0,
            "total_whale_pnl": 0.0,
            "avg_win_rate": 0.0,
            "total_positions": 0,
            "total_volume": 0.0,
            "action_breakdown": {
                "new_entries": 0,
                "size_increases": 0,
                "size_decreases": 0,
                "exits": 0,
            },
            "direction_distribution": {"bullish": 0, "bearish": 0},
            "strength_distribution": {"STRONG": 0, "MODERATE": 0, "WEAK": 0},
            "freshness_distribution": {},
            "top_conviction_market": "—",
            "top_conviction_score": 0,
        },
    }


# ─── Scanner Whale Profile Builder (live Data API) ─────────────────

def _build_scanner_whale_profile(address: str, candidate: dict | None, conn) -> Any:
    """Build a rich whale profile using live Data API data.
    Works for scanner candidates and any arbitrary address.
    """
    import asyncio
    from datetime import datetime, timezone
    from src.connectors.polymarket_data import DataAPIClient

    # ── Fetch live data from Data API ──
    live_positions: list[dict] = []
    live_activity: list[dict] = []
    fetch_error = None

    async def _fetch_live():
        client = DataAPIClient()
        try:
            positions = await client.get_positions(address, sort_by="CURRENT", limit=200)
            activity = await client.get_activity(address, limit=200)
            return positions, activity
        finally:
            await client.close()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        positions_raw, activity_raw = loop.run_until_complete(_fetch_live())
        loop.close()
        live_positions = [p.to_dict() for p in positions_raw]
        live_activity = [a.to_dict() for a in activity_raw]
    except Exception as e:
        fetch_error = str(e)[:200]
        positions_raw = []
        activity_raw = []

    # ── Compute stats from live positions ──
    total_pnl = sum(p.cash_pnl for p in positions_raw)
    total_invested = sum(p.initial_value for p in positions_raw if p.initial_value > 0)
    total_current = sum(p.current_value for p in positions_raw)
    winners = sum(1 for p in positions_raw if p.cash_pnl > 0)
    losers = sum(1 for p in positions_raw if p.cash_pnl < 0)
    total_count = len(positions_raw)
    win_rate = winners / total_count if total_count > 0 else 0
    avg_pos_size = total_invested / total_count if total_count > 0 else 0
    biggest_position = max((p.current_value for p in positions_raw), default=0)
    best_pnl = max((p.cash_pnl for p in positions_raw), default=0)
    worst_pnl = min((p.cash_pnl for p in positions_raw), default=0)

    # ── Compute average holding time ──
    now = datetime.now(timezone.utc)
    holding_days: list[float] = []
    for p in positions_raw:
        if p.end_date:
            try:
                # end_date can be ISO timestamp
                ed = p.end_date.replace("Z", "+00:00")
                end_dt = datetime.fromisoformat(ed)
                delta = (end_dt - now).days
                if delta > 0:
                    holding_days.append(float(delta))
            except Exception:
                pass
    avg_holding_days = sum(holding_days) / len(holding_days) if holding_days else 0
    # Also measure from activity timestamps
    activity_timestamps: list[datetime] = []
    for a in activity_raw:
        if a.timestamp:
            try:
                ts = a.timestamp.replace("Z", "+00:00") if isinstance(a.timestamp, str) else str(a.timestamp)
                activity_timestamps.append(datetime.fromisoformat(ts))
            except Exception:
                pass
    trading_span_days = 0
    if len(activity_timestamps) >= 2:
        sorted_ts = sorted(activity_timestamps)
        trading_span_days = (sorted_ts[-1] - sorted_ts[0]).days

    # ── Realized vs unrealized ──
    realized_positions = [p for p in positions_raw if p.realized]
    unrealized_positions = [p for p in positions_raw if not p.realized]
    realized_pnl = sum(p.cash_pnl for p in realized_positions)
    unrealized_pnl = sum(p.cash_pnl for p in unrealized_positions)

    # ── Outcome distribution (Yes vs No) ──
    yes_count = sum(1 for p in positions_raw if p.outcome.lower() == "yes")
    no_count = sum(1 for p in positions_raw if p.outcome.lower() == "no")

    # ── Top markets by current value ──
    top_markets = sorted(
        [{"title": p.title[:60], "market_slug": p.market_slug, "outcome": p.outcome,
          "current_value": p.current_value, "cash_pnl": p.cash_pnl,
          "size": p.size, "avg_price": p.avg_price, "cur_price": p.cur_price,
          "total_usd": p.current_value, "direction": "BULLISH" if p.outcome.lower() == "yes" else "BEARISH",
          "conviction": min(100, (p.current_value / max(avg_pos_size, 1)) * 50)}
         for p in positions_raw if p.current_value > 0],
        key=lambda x: x["current_value"], reverse=True,
    )[:15]

    # ── Category distribution from market titles (heuristic) ──
    cat_keywords = {
        "Politics": ["president", "election", "trump", "biden", "congress", "senate", "governor", "political", "vote"],
        "Crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "token", "defi", "solana"],
        "Sports": ["nba", "nfl", "mlb", "soccer", "football", "basketball", "ufc", "fight", "championship", "super bowl", "world cup"],
        "Economy": ["gdp", "inflation", "fed", "interest rate", "recession", "unemployment", "stock", "s&p"],
        "Entertainment": ["oscar", "grammy", "movie", "show", "celebrity", "award"],
        "Science": ["ai", "artificial intelligence", "space", "nasa", "climate", "weather"],
    }
    cat_dist: dict[str, int] = {}
    for p in positions_raw:
        t = p.title.lower()
        matched = False
        for cat, keywords in cat_keywords.items():
            if any(k in t for k in keywords):
                cat_dist[cat] = cat_dist.get(cat, 0) + 1
                matched = True
                break
        if not matched:
            cat_dist["Other"] = cat_dist.get("Other", 0) + 1

    # ── Monthly activity timeline ──
    monthly_pnl: dict[str, dict] = {}
    for a in activity_raw:
        ts = a.timestamp if isinstance(a.timestamp, str) else str(a.timestamp)
        month_key = ts[:7] if len(ts) >= 7 else "unknown"
        if month_key == "unknown":
            continue
        if month_key not in monthly_pnl:
            monthly_pnl[month_key] = {"month": month_key, "entries": 0, "exits": 0, "volume": 0.0, "net_flow": 0.0}
        val = abs(a.value_usd)
        monthly_pnl[month_key]["volume"] += val
        action_lower = a.action.lower()
        if action_lower in ("buy", "mint"):
            monthly_pnl[month_key]["entries"] += 1
            monthly_pnl[month_key]["net_flow"] += val
        elif action_lower in ("sell", "redeem"):
            monthly_pnl[month_key]["exits"] += 1
            monthly_pnl[month_key]["net_flow"] -= val
    monthly_timeline = sorted(monthly_pnl.values(), key=lambda x: x["month"])

    # ── Build wallet object (compatible with tracked wallet format) ──
    name = ""
    tier = "RISING"
    score = 0
    grade = "C"
    scan_data = {}

    if candidate:
        name = candidate.get("name", "")
        score = candidate.get("score", 0)
        grade = candidate.get("grade", "C")
        tier_map = {"S": "LEGENDARY", "A": "ELITE", "B": "PRO", "C": "RISING", "D": "RISING", "F": "RISING"}
        tier = tier_map.get(grade, "RISING")
        try:
            scan_data = json.loads(candidate.get("scan_data_json", "{}"))
        except Exception:
            scan_data = {}

    w = {
        "address": address,
        "name": name or address[:12],
        "tier": tier,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "total_volume": round(total_invested, 2),
        "active_positions": total_count,
        "score": round(score, 1),
        "grade": grade,
        "source": "scanner_candidate" if candidate else "live_lookup",
        "avg_position_size": round(avg_pos_size, 2),
        "biggest_position": round(biggest_position, 2),
    }

    # ── Is starred? ──
    star_row = conn.execute(
        "SELECT id FROM whale_stars WHERE star_type='whale' AND identifier=?", (address,)
    ).fetchone()

    # ── Signals (built from live positions for compatibility) ──
    signals = [
        {"market_slug": p.market_slug, "title": p.title[:60], "outcome": p.outcome,
         "direction": "BULLISH" if p.outcome.lower() == "yes" else "BEARISH",
         "total_whale_usd": round(p.current_value, 2),
         "conviction_score": round(min(100, (p.current_value / max(avg_pos_size, 1)) * 50)),
         "current_price": round(p.cur_price, 4)}
        for p in positions_raw if p.current_value > 1
    ]
    signals.sort(key=lambda s: s["total_whale_usd"], reverse=True)

    # ── Deltas (built from live activity for compatibility) ──
    deltas = [
        {"action": _map_activity_action(a.action), "market_slug": a.market_slug,
         "title": a.title[:60], "outcome": a.outcome,
         "value_change_usd": round(a.value_usd, 2),
         "wallet_address": address, "wallet_name": name or address[:12],
         "detected_at": a.timestamp}
        for a in activity_raw
    ]

    bullish = sum(1 for s in signals if s.get("direction") == "BULLISH")
    bearish = len(signals) - bullish

    return jsonify({
        "wallet": w,
        "signals": signals[:50],
        "deltas": deltas[:100],
        "monthly_timeline": monthly_timeline,
        "months_available": len(monthly_timeline),
        "total_signals": len(signals),
        "bullish_signals": bullish,
        "bearish_signals": bearish,
        "top_markets": top_markets[:10],
        "category_distribution": cat_dist,
        "is_starred": bool(star_row),
        # ── Enhanced live data (scanner profile extras) ──
        "is_scanner_profile": True,
        "live_positions": live_positions[:50],
        "live_activity": live_activity[:100],
        "live_stats": {
            "total_positions": total_count,
            "winners": winners,
            "losers": losers,
            "win_rate_pct": round(win_rate * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_invested": round(total_invested, 2),
            "total_current_value": round(total_current, 2),
            "avg_position_size": round(avg_pos_size, 2),
            "biggest_position": round(biggest_position, 2),
            "best_trade_pnl": round(best_pnl, 2),
            "worst_trade_pnl": round(worst_pnl, 2),
            "avg_holding_days": round(avg_holding_days, 1),
            "trading_span_days": trading_span_days,
            "realized_count": len(realized_positions),
            "unrealized_count": len(unrealized_positions),
            "yes_positions": yes_count,
            "no_positions": no_count,
        },
        "scan_data": scan_data,
        "fetch_error": fetch_error,
    })


def _map_activity_action(action: str) -> str:
    """Map Data API activity action to whale delta action format."""
    a = action.lower()
    if a in ("buy", "mint"):
        return "NEW_ENTRY"
    elif a in ("sell",):
        return "SIZE_DECREASE"
    elif a in ("redeem",):
        return "EXIT"
    return action.upper()


# ─── API: Whale Profile ───────────────────────────────────────────

@app.route("/api/whale-profile/<address>")
def api_whale_profile(address: str) -> Any:
    """Full profile for a single whale: stats, signals, deltas, back-analysis.
    Works for BOTH tracked wallets AND scanner-discovered candidates.
    If not found in tracked_wallets, falls through to whale_candidates + live Data API.
    """
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Basic wallet info — check tracked_wallets first
        wallet = conn.execute(
            "SELECT * FROM tracked_wallets WHERE address = ?", (address,)
        ).fetchone()
        is_tracked = wallet is not None

        if not is_tracked:
            # Fall through to whale_candidates + live Data API
            candidate = conn.execute(
                "SELECT * FROM whale_candidates WHERE address = ?", (address,)
            ).fetchone()
            return _build_scanner_whale_profile(address, dict(candidate) if candidate else None, conn)

        w = dict(wallet)

        # All signals where this whale participates
        signals = []
        all_sigs = conn.execute("SELECT * FROM wallet_signals ORDER BY conviction_score DESC").fetchall()
        for s in all_sigs:
            sd = dict(s)
            names_json = sd.get("whale_names_json") or "[]"
            try:
                whale_names = json.loads(names_json)
            except (json.JSONDecodeError, TypeError):
                whale_names = []
            # Check if this whale is in the signal
            if w.get("name") in whale_names or address in str(names_json):
                sd["whale_names"] = whale_names
                signals.append(sd)

        # All deltas for this whale
        deltas = [
            dict(r) for r in conn.execute(
                "SELECT * FROM wallet_deltas WHERE wallet_address = ? ORDER BY detected_at DESC",
                (address,),
            ).fetchall()
        ]

        # ── Back-analysis: compute monthly PnL timeline ──
        monthly_pnl: dict[str, dict] = {}
        for d in deltas:
            ts = d.get("detected_at") or ""
            month_key = ts[:7] if len(ts) >= 7 else "unknown"
            if month_key not in monthly_pnl:
                monthly_pnl[month_key] = {"month": month_key, "entries": 0, "exits": 0, "volume": 0.0, "net_flow": 0.0}
            action = d.get("action", "")
            val = abs(d.get("value_change_usd", 0) or 0)
            monthly_pnl[month_key]["volume"] += val
            if action in ("NEW_ENTRY", "SIZE_INCREASE"):
                monthly_pnl[month_key]["entries"] += 1
                monthly_pnl[month_key]["net_flow"] += val
            elif action in ("EXIT", "SIZE_DECREASE"):
                monthly_pnl[month_key]["exits"] += 1
                monthly_pnl[month_key]["net_flow"] -= val
        monthly_timeline = sorted(monthly_pnl.values(), key=lambda x: x["month"])

        # ── Win/Loss distribution from signals ──
        total_signals = len(signals)
        bullish = sum(1 for s in signals if s.get("direction") == "BULLISH")
        bearish = total_signals - bullish

        # ── Top markets by capital ──
        market_positions = {}
        for s in signals:
            slug = s.get("market_slug", "")
            if slug not in market_positions:
                market_positions[slug] = {
                    "title": (s.get("title") or slug)[:60],
                    "total_usd": 0, "direction": s.get("direction", ""),
                    "conviction": s.get("conviction_score", 0),
                    "outcome": s.get("outcome", ""),
                    "current_price": s.get("current_price", 0),
                }
            market_positions[slug]["total_usd"] += s.get("total_whale_usd", 0) or 0
        top_markets = sorted(market_positions.values(), key=lambda x: x["total_usd"], reverse=True)[:10]

        # ── Category distribution ──
        cat_dist: dict[str, int] = {}
        for s in signals:
            cat = s.get("category") or "Other"
            cat_dist[cat] = cat_dist.get(cat, 0) + 1

        # ── Is starred? ──
        star_row = conn.execute(
            "SELECT id FROM whale_stars WHERE star_type='whale' AND identifier=?", (address,)
        ).fetchone()

        return jsonify({
            "wallet": w,
            "signals": signals[:50],
            "deltas": deltas[:100],
            "monthly_timeline": monthly_timeline,
            "months_available": len(monthly_timeline),
            "total_signals": total_signals,
            "bullish_signals": bullish,
            "bearish_signals": bearish,
            "top_markets": top_markets,
            "category_distribution": cat_dist,
            "is_starred": bool(star_row),
        })
    finally:
        conn.close()


# ─── API: Market Detail ───────────────────────────────────────────

@app.route("/api/market-detail/<path:slug>")
def api_market_detail(slug: str) -> Any:
    """Detail view for a market: signals, whale activity, links."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Get all signals for this market
        signals = [
            dict(r) for r in conn.execute(
                "SELECT * FROM wallet_signals WHERE market_slug = ? ORDER BY conviction_score DESC",
                (slug,),
            ).fetchall()
        ]
        for s in signals:
            try:
                s["whale_names"] = json.loads(s.get("whale_names_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                s["whale_names"] = []

        # Get deltas for this market
        deltas = [
            dict(r) for r in conn.execute(
                "SELECT * FROM wallet_deltas WHERE market_slug = ? ORDER BY detected_at DESC",
                (slug,),
            ).fetchall()
        ]

        # Market metadata from markets table
        market_info = conn.execute(
            "SELECT * FROM markets WHERE id = ? OR condition_id = ?", (slug, slug)
        ).fetchone()
        market_data = dict(market_info) if market_info else {}

        # Aggregate stats
        total_whale_usd = sum(s.get("total_whale_usd", 0) or 0 for s in signals)
        whale_count = len(set(
            name for s in signals for name in s.get("whale_names", [])
        ))
        avg_conviction = (
            sum(s.get("conviction_score", 0) for s in signals) / len(signals)
        ) if signals else 0

        # Polymarket direct link
        polymarket_url = f"https://polymarket.com/event/{slug}"
        condition_id = signals[0].get("condition_id", "") if signals else market_data.get("condition_id", "")

        # Is starred?
        star_row = conn.execute(
            "SELECT id FROM whale_stars WHERE star_type='market' AND identifier=?", (slug,)
        ).fetchone()

        # Recent entries vs exits
        entries = sum(1 for d in deltas if d.get("action") in ("NEW_ENTRY", "SIZE_INCREASE"))
        exits = sum(1 for d in deltas if d.get("action") in ("EXIT", "SIZE_DECREASE"))

        return jsonify({
            "slug": slug,
            "market": market_data,
            "signals": signals[:30],
            "deltas": deltas[:50],
            "total_whale_usd": round(total_whale_usd, 2),
            "whale_count": whale_count,
            "avg_conviction": round(avg_conviction, 1),
            "polymarket_url": polymarket_url,
            "condition_id": condition_id,
            "entries": entries,
            "exits": exits,
            "is_starred": bool(star_row),
            "title": signals[0].get("title", slug) if signals else market_data.get("question", slug),
        })
    finally:
        conn.close()


# ─── API: Whale Stars (Watchlist) ─────────────────────────────────

@app.route("/api/whale-stars", methods=["GET"])
def api_whale_stars_list() -> Any:
    """List all starred items."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute("SELECT * FROM whale_stars ORDER BY starred_at DESC").fetchall()
        return jsonify({"stars": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/whale-stars", methods=["POST"])
def api_whale_stars_add() -> Any:
    """Star/unstar an item. Body: {star_type, identifier, label}"""
    data = request.get_json(force=True)
    star_type = data.get("star_type", "")  # 'whale' or 'market'
    identifier = data.get("identifier", "")
    label = data.get("label", "")
    if not star_type or not identifier:
        return jsonify({"error": "star_type and identifier required"}), 400

    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Toggle: if exists → delete, else → insert
        existing = conn.execute(
            "SELECT id FROM whale_stars WHERE star_type=? AND identifier=?",
            (star_type, identifier),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM whale_stars WHERE star_type=? AND identifier=?",
                (star_type, identifier),
            )
            conn.commit()
            return jsonify({"action": "unstarred", "star_type": star_type, "identifier": identifier})
        else:
            conn.execute(
                "INSERT INTO whale_stars (star_type, identifier, label) VALUES (?, ?, ?)",
                (star_type, identifier, label),
            )
            conn.commit()
            return jsonify({"action": "starred", "star_type": star_type, "identifier": identifier})
    finally:
        conn.close()


# ─── API: Strategy Mentor (LLM Chat) ──────────────────────────────

@app.route("/api/whale-mentor", methods=["POST"])
def api_whale_mentor() -> Any:
    """Chat with the Strategy Mentor agent.

    Body: {message, whale_address (optional), context_type}
    Uses OpenAI (or configured LLM) to analyze whale data and respond.
    """
    data = request.get_json(force=True)
    user_message = data.get("message", "").strip()
    whale_address = data.get("whale_address", "")
    if not user_message:
        return jsonify({"error": "message is required"}), 400

    # Build whale context
    whale_context = ""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        if whale_address:
            wallet = conn.execute(
                "SELECT * FROM tracked_wallets WHERE address = ?", (whale_address,)
            ).fetchone()
            if wallet:
                w = dict(wallet)
                whale_context += f"\n## Whale Profile: {w.get('name', 'Unknown')}\n"
                whale_context += f"- Address: {whale_address}\n"
                whale_context += f"- Total P&L: ${w.get('total_pnl', 0):,.0f}\n"
                whale_context += f"- Win Rate: {(w.get('win_rate', 0) * 100):.1f}%\n"
                whale_context += f"- Active Positions: {w.get('active_positions', 0)}\n"
                whale_context += f"- Total Volume: ${w.get('total_volume', 0):,.0f}\n"
                whale_context += f"- Score: {w.get('score', 0)}\n"
                whale_context += f"- Tier: {w.get('tier', 'RISING')}\n\n"

            # Their signals
            all_sigs = conn.execute(
                "SELECT * FROM wallet_signals ORDER BY conviction_score DESC"
            ).fetchall()
            whale_signals = []
            for s in all_sigs:
                sd = dict(s)
                names_json = sd.get("whale_names_json") or "[]"
                try:
                    whale_names = json.loads(names_json)
                except (json.JSONDecodeError, TypeError):
                    whale_names = []
                wallet_name = w.get("name", "") if wallet else ""
                if wallet_name in whale_names or whale_address in str(names_json):
                    whale_signals.append(sd)

            if whale_signals:
                whale_context += "## Current Positions/Signals:\n"
                for s in whale_signals[:15]:
                    whale_context += f"- {s.get('title', '')[:50]} | {s.get('outcome', '')} | "
                    whale_context += f"${s.get('total_whale_usd', 0):,.0f} | "
                    whale_context += f"Conviction: {s.get('conviction_score', 0)} | "
                    whale_context += f"Direction: {s.get('direction', '')}\n"

            # Their recent activity
            deltas = conn.execute(
                "SELECT * FROM wallet_deltas WHERE wallet_address = ? ORDER BY detected_at DESC LIMIT 20",
                (whale_address,),
            ).fetchall()
            if deltas:
                whale_context += "\n## Recent Trading Activity:\n"
                for d in deltas:
                    dd = dict(d)
                    whale_context += f"- {dd.get('action', '')} | {(dd.get('title', '') or '')[:40]} | "
                    whale_context += f"${abs(dd.get('value_change_usd', 0) or 0):,.0f} | "
                    whale_context += f"{dd.get('detected_at', '')}\n"
        else:
            # General whale market overview
            wallets = conn.execute(
                "SELECT * FROM tracked_wallets ORDER BY score DESC LIMIT 15"
            ).fetchall()
            if wallets:
                whale_context += "## Top Whales Overview:\n"
                for w in wallets:
                    wd = dict(w)
                    whale_context += f"- {wd.get('name', '—')}: P&L ${wd.get('total_pnl', 0):,.0f}, "
                    whale_context += f"WR {(wd.get('win_rate', 0) * 100):.1f}%, "
                    whale_context += f"Score {wd.get('score', 0)}\n"

            signals = conn.execute(
                "SELECT * FROM wallet_signals ORDER BY conviction_score DESC LIMIT 15"
            ).fetchall()
            if signals:
                whale_context += "\n## Top Conviction Signals:\n"
                for s in signals:
                    sd = dict(s)
                    whale_context += f"- {sd.get('title', '')[:40]} | {sd.get('direction', '')} | "
                    whale_context += f"Conviction {sd.get('conviction_score', 0)} | "
                    whale_context += f"${sd.get('total_whale_usd', 0):,.0f}\n"

        # Save user message to conversation history
        conn.execute(
            "INSERT INTO mentor_conversations (whale_address, role, content) VALUES (?, 'user', ?)",
            (whale_address or "", user_message),
        )
        conn.commit()

        # Get recent conversation history for context
        history_rows = conn.execute(
            "SELECT role, content FROM mentor_conversations WHERE whale_address = ? ORDER BY id DESC LIMIT 10",
            (whale_address or "",),
        ).fetchall()
        history = list(reversed([dict(r) for r in history_rows]))

    finally:
        conn.close()

    # ── Call LLM ──
    system_prompt = f"""You are the Strategy Mentor, an expert trading analyst embedded in a Polymarket whale tracking dashboard.
You analyze whale (large trader) behavior on Polymarket, a prediction market platform.

Your role:
- Analyze whale trading patterns, entries, exits, and positioning
- Identify strategic patterns: accumulation, distribution, momentum shifts
- Provide actionable insights on whale behavior and market sentiment
- Explain whale strategies in plain language
- Warn about risks: herd behavior, concentration, stale positions
- Compare whale performance and identify the most skilled traders

Be concise, use data from the context, and give specific actionable insights.
Use trading terminology naturally. Reference specific whales, markets, and numbers.
Format responses with markdown for readability.

{whale_context}"""

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[:-1]:  # Skip the last (current) user message since we add it explicitly
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    try:
        import openai
        client = openai.OpenAI()
        cfg = _get_config()
        model = cfg.forecasting.llm_model if cfg else "gpt-4o"
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=1500,
            temperature=0.7,
        )
        assistant_reply = response.choices[0].message.content or "I couldn't generate a response."
    except Exception as e:
        assistant_reply = f"⚠️ LLM error: {str(e)}\n\nMake sure OPENAI_API_KEY is set in your environment."

    # Save assistant reply
    conn2 = _get_conn()
    try:
        conn2.execute(
            "INSERT INTO mentor_conversations (whale_address, role, content) VALUES (?, 'assistant', ?)",
            (whale_address or "", assistant_reply),
        )
        conn2.commit()
    finally:
        conn2.close()

    return jsonify({"reply": assistant_reply, "model": model if 'model' in dir() else "gpt-4o"})


@app.route("/api/whale-mentor/history", methods=["GET"])
def api_whale_mentor_history() -> Any:
    """Get chat history for a whale or general."""
    whale_address = request.args.get("whale_address", "")
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute(
            "SELECT role, content, created_at FROM mentor_conversations WHERE whale_address = ? ORDER BY id ASC LIMIT 50",
            (whale_address,),
        ).fetchall()
        return jsonify({"history": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/whale-mentor/clear", methods=["POST"])
def api_whale_mentor_clear() -> Any:
    """Clear chat history."""
    data = request.get_json(force=True)
    whale_address = data.get("whale_address", "")
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM mentor_conversations WHERE whale_address = ?", (whale_address,))
        conn.commit()
        return jsonify({"cleared": True})
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════
#  LIQUID MARKET WHALE SCANNER  v4 — Multi-Source Discovery
# ═══════════════════════════════════════════════════════════════════
# Runs continuously when activated – no idle waits between cycles.
# Discovery sources:
#   1. Leaderboard API (top 50 profit + top 50 volume wallets)
#   2. Global trade feed (Data API /trades with rotating offset)
#   3. Per-market trade scanning (top 10 liquid markets)
#   4. Smart dedup — skips recently-analysed addresses for 10 iters

_liquid_scan_thread: threading.Thread | None = None
_liquid_scan_stop = threading.Event()

# Persistent accumulators (survive across iterations, reset on restart)
_scan_address_stats: dict[str, dict] = {}
_scan_total_trades: int = 0
_scan_total_unique: int = 0
_scan_iteration: int = 0
_scan_last_offset: int = 0
_scan_status_text: str = "idle"

DATA_API_TRADES_URL = "https://data-api.polymarket.com/trades"
LEADERBOARD_API_URL = "https://lb-api.polymarket.com"

# Persistent dedup: tracks when each address was last deep-scanned
_scan_deep_history: dict[str, int] = {}   # addr -> iteration# last scanned
_scan_leaderboard_seeded: int = 0         # how many leaderboard wallets seeded
_scan_market_trades: int = 0              # trades found via per-market scanning

# ── API Pool (initialized on first scanner start) ────────────────────
from src.connectors.api_pool import ApiPool, load_pool_from_config
_scanner_api_pool: ApiPool | None = None


def _get_scanner_pool() -> ApiPool:
    """Lazy-initialize the scanner API pool."""
    global _scanner_api_pool
    if _scanner_api_pool is None:
        _scanner_api_pool = load_pool_from_config()
    return _scanner_api_pool


async def _fetch_leaderboard_wallets() -> list[dict]:
    """Fetch top profit + volume wallets from the Polymarket Leaderboard API.

    Routes through the API pool for rate limiting & failover.
    Returns up to 100 unique wallet entries (50 profit, 50 volume).
    Each entry: {proxyWallet, amount, name, pseudonym, source}.
    """
    import httpx as _hx
    pool = _get_scanner_pool()
    results: dict[str, dict] = {}
    for endpoint in ("profit", "volume"):
        try:
            # Leaderboard API is on a different domain, use direct httpx
            # but still respect the pool's rate limiter on the primary endpoint
            ep = pool.endpoints[0] if pool.endpoints else None
            if ep:
                await ep.limiter.acquire()

            async with _hx.AsyncClient(timeout=20.0) as cli:
                resp = await cli.get(
                    f"{LEADERBOARD_API_URL}/{endpoint}",
                    params={"limit": 50, "window": "all"},
                )
                resp.raise_for_status()
                data = resp.json()
                if ep:
                    ep.record_success()
                if isinstance(data, list):
                    for entry in data:
                        addr = str(entry.get("proxyWallet", "")).lower().strip()
                        if not addr or len(addr) < 10:
                            continue
                        if addr not in results:
                            results[addr] = {
                                "proxyWallet": addr,
                                "amount": float(entry.get("amount", 0)),
                                "name": entry.get("name", "") or entry.get("pseudonym", "") or "",
                                "source": f"leaderboard_{endpoint}",
                            }
                        else:
                            results[addr]["source"] = "leaderboard_both"
        except Exception:
            pass
    return list(results.values())


async def _fetch_market_trades(slug: str, limit: int = 200) -> list[dict]:
    """Fetch trades for a specific market by slug — routed through API pool."""
    pool = _get_scanner_pool()
    data = await pool.get("/trades", params={"slug": slug, "limit": limit}, timeout=20.0)
    return data if isinstance(data, list) else []


async def _fetch_discovery_trades(limit: int = 500, offset: int = 0) -> list[dict]:
    """Fetch recent global trades — routed through API pool."""
    pool = _get_scanner_pool()
    data = await pool.get("/trades", params={"limit": limit, "offset": offset}, timeout=30.0)
    return data if isinstance(data, list) else []


async def _run_continuous_scan_iteration() -> dict:
    """Run a single iteration of the continuous scanner.

    Each iteration:
      1. Discover liquid markets (all with >= $10K liquidity)
      2. Fetch a fresh page of global trades & accumulate address stats
      3. When enough addresses accumulated, deep-analyse the top ones
      4. Score & save new candidates to DB
    """
    import asyncio
    from datetime import datetime, timezone
    from src.connectors.polymarket_gamma import GammaClient
    from src.connectors.polymarket_data import DataAPIClient

    global _scan_address_stats, _scan_total_trades, _scan_total_unique
    global _scan_iteration, _scan_last_offset, _scan_status_text

    conn = _get_conn()
    _ensure_tables(conn)
    iter_start = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()
    _scan_iteration += 1

    cfg_row = conn.execute("SELECT * FROM whale_scan_config WHERE id = 1").fetchone()
    cfg = dict(cfg_row) if cfg_row else {}
    min_volume = cfg.get("min_volume", 10000)
    min_liquidity = cfg.get("min_liquidity", 10000)
    min_win_rate = cfg.get("min_win_rate", 0.35)
    min_pnl = cfg.get("min_pnl", 1000)
    min_positions = cfg.get("min_positions", 3)
    max_candidates = cfg.get("max_candidates", 50)

    result: dict[str, Any] = {
        "status": "running",
        "iteration": _scan_iteration,
        "liquid_markets": [],
        "trades_analyzed": 0,
        "unique_addresses": 0,
        "wallets_scanned": 0,
        "candidates_found": 0,
        "leaderboard_seeded": 0,
        "market_trades_found": 0,
        "phase_timings": {},
        "errors": [],
    }

    # ─── Phase 0: Leaderboard Seeding (every 10 iterations) ─────
    global _scan_leaderboard_seeded, _scan_market_trades
    p0_start = time.time()
    _scan_status_text = "seeding leaderboard"

    if _scan_iteration == 1 or _scan_iteration % 10 == 0:
        lb_wallets = await _fetch_leaderboard_wallets()
        seeded_count = 0
        for lw in lb_wallets:
            addr = lw["proxyWallet"]
            if addr not in _scan_address_stats:
                _scan_address_stats[addr] = {
                    "total_volume": lw["amount"],
                    "trade_count": 0,
                    "markets": set(),
                    "biggest_trade": 0.0,
                    "name": lw["name"],
                    "in_liquid": 0,
                    "first_seen": _scan_iteration,
                    "source": lw["source"],
                }
                seeded_count += 1
            else:
                # Update name if leaderboard has a better one
                if lw["name"] and len(lw["name"]) > len(_scan_address_stats[addr].get("name", "")):
                    _scan_address_stats[addr]["name"] = lw["name"]
                # Upgrade source to leaderboard
                if _scan_address_stats[addr].get("source", "") == "trade_discovery":
                    _scan_address_stats[addr]["source"] = lw["source"]
        _scan_leaderboard_seeded += seeded_count
        result["leaderboard_seeded"] = seeded_count

    result["phase_timings"]["leaderboard_seeding"] = round(time.time() - p0_start, 2)

    # ─── Phase 1: Market Discovery (every 5 iterations) ─────────
    p1_start = time.time()
    _scan_status_text = "discovering markets"
    all_liquid_markets: list[dict] = []

    if _scan_iteration == 1 or _scan_iteration % 5 == 0:
        gamma = GammaClient()
        try:
            seen_ids: set[str] = set()
            for batch_offset in range(0, 500, 100):
                for order_by in ("volume", "liquidity"):
                    try:
                        page = await gamma.list_markets(
                            limit=100, offset=batch_offset,
                            active=True, closed=False,
                            order=order_by, ascending=False,
                        )
                        for m in page:
                            if m.id in seen_ids:
                                continue
                            if m.liquidity < min_liquidity:
                                continue
                            if not m.tokens:
                                continue
                            seen_ids.add(m.id)
                            all_liquid_markets.append({
                                "id": m.id, "slug": m.slug,
                                "question": m.question[:80],
                                "category": m.category or m.market_type,
                                "volume": round(m.volume, 2),
                                "liquidity": round(m.liquidity, 2),
                                "best_bid": round(m.best_bid, 4),
                                "spread": round(m.spread, 4),
                            })
                    except Exception as e:
                        result["errors"].append(f"Gamma {batch_offset}/{order_by}: {str(e)[:60]}")
                if _liquid_scan_stop.is_set():
                    break
        except Exception as e:
            result["errors"].append(f"Market discovery: {str(e)[:100]}")
        finally:
            await gamma.close()

        all_liquid_markets.sort(key=lambda x: x["volume"], reverse=True)
        # Store in DB for status endpoint
        markets_json = json.dumps([
            {"slug": m["slug"], "question": m["question"],
             "volume": m["volume"], "liquidity": m["liquidity"]}
            for m in all_liquid_markets[:50]
        ])
        conn.execute(
            "UPDATE whale_scan_config SET last_scan_markets_json = ? WHERE id = 1",
            (markets_json,),
        )
        conn.commit()

    result["liquid_markets"] = all_liquid_markets
    phase_timings_p1 = round(time.time() - p1_start, 2)
    result["phase_timings"]["market_discovery"] = phase_timings_p1

    # Build liquid slug set from latest stored markets
    try:
        stored = json.loads(cfg.get("last_scan_markets_json", "[]"))
        liquid_slugs = {m["slug"] for m in stored if isinstance(m, dict)}
    except Exception:
        liquid_slugs = {m["slug"] for m in all_liquid_markets}

    # ─── Phase 2: Trade Scanning (continuous accumulation) ───────
    p2_start = time.time()
    _scan_status_text = "scanning trades"
    page_trades = 0

    # Fetch 4 pages of 500 per iteration (2000 trades) with rotating offset
    for _ in range(4):
        if _liquid_scan_stop.is_set():
            break
        raw = await _fetch_discovery_trades(limit=500, offset=_scan_last_offset)
        if not raw:
            _scan_last_offset = 0  # reset to newest
            break
        _scan_last_offset += 500
        if _scan_last_offset > 10000:
            _scan_last_offset = 0

        for t in raw:
            _scan_total_trades += 1
            page_trades += 1
            addr = str(t.get("proxyWallet", "")).lower().strip()
            if not addr or len(addr) < 10 or addr == "0x0000000000000000000000000000000000000000":
                continue

            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
            value = size * price if price > 0 else size
            slug = t.get("slug", "") or ""
            name = t.get("name", "") or t.get("pseudonym", "") or ""

            if addr not in _scan_address_stats:
                _scan_address_stats[addr] = {
                    "total_volume": 0.0, "trade_count": 0,
                    "markets": set(), "biggest_trade": 0.0,
                    "name": name, "in_liquid": 0,
                    "first_seen": _scan_iteration,
                }
            s = _scan_address_stats[addr]
            s["total_volume"] += value
            s["trade_count"] += 1
            if slug:
                s["markets"].add(slug)
                if slug in liquid_slugs:
                    s["in_liquid"] += 1
            if value > s["biggest_trade"]:
                s["biggest_trade"] = value
            if name and len(name) > len(s.get("name", "")):
                s["name"] = name

        await asyncio.sleep(0.25)

    _scan_total_unique = len(_scan_address_stats)
    result["trades_analyzed"] = page_trades
    result["unique_addresses"] = _scan_total_unique
    result["phase_timings"]["trade_scanning"] = round(time.time() - p2_start, 2)

    # ─── Phase 2b: Per-Market Trade Scanning ─────────────────────
    # Fetch trades from the top liquid markets to find whales active
    # in specific high-value markets (may not appear in global feed).
    p2b_start = time.time()
    _scan_status_text = "scanning market trades"
    market_trades_found = 0

    # Every 3 iterations, scan trades from top 10 liquid markets
    if _scan_iteration % 3 == 0 and liquid_slugs:
        try:
            stored_mkts = json.loads(cfg.get("last_scan_markets_json", "[]"))
            top_liquid = sorted(stored_mkts, key=lambda m: m.get("volume", 0), reverse=True)[:10]
        except Exception:
            top_liquid = []

        for mkt in top_liquid:
            if _liquid_scan_stop.is_set():
                break
            slug = mkt.get("slug", "")
            if not slug:
                continue
            raw = await _fetch_market_trades(slug, limit=200)
            for t in raw:
                addr = str(t.get("proxyWallet", "")).lower().strip()
                if not addr or len(addr) < 10:
                    continue
                size = float(t.get("size", 0))
                price = float(t.get("price", 0))
                value = size * price if price > 0 else size
                name = t.get("name", "") or t.get("pseudonym", "") or ""

                if addr not in _scan_address_stats:
                    _scan_address_stats[addr] = {
                        "total_volume": 0.0, "trade_count": 0,
                        "markets": set(), "biggest_trade": 0.0,
                        "name": name, "in_liquid": 0,
                        "first_seen": _scan_iteration,
                        "source": "market_scan",
                    }
                s = _scan_address_stats[addr]
                s["total_volume"] += value
                s["trade_count"] += 1
                if slug:
                    s["markets"].add(slug)
                    s["in_liquid"] += 1
                if value > s["biggest_trade"]:
                    s["biggest_trade"] = value
                if name and len(name) > len(s.get("name", "")):
                    s["name"] = name
                market_trades_found += 1
            await asyncio.sleep(0.3)

    _scan_market_trades += market_trades_found
    _scan_total_unique = len(_scan_address_stats)
    result["market_trades_found"] = market_trades_found
    result["phase_timings"]["market_trade_scanning"] = round(time.time() - p2b_start, 2)

    # ─── Phase 3: Address Ranking ────────────────────────────────
    p3_start = time.time()
    _scan_status_text = "ranking addresses"
    global _scan_deep_history

    # Include leaderboard wallets even if they don't meet trade_count threshold
    ranked = [
        (a, s) for a, s in _scan_address_stats.items()
        if (s["total_volume"] >= 100 and s["trade_count"] >= 2)
        or s.get("source", "").startswith("leaderboard")
    ]
    ranked.sort(key=lambda x: x[1]["total_volume"], reverse=True)
    top_addrs = ranked[:150]

    # Merge known/promoted wallets
    known_addrs: set[str] = set()
    try:
        for row in conn.execute("SELECT address FROM tracked_wallets").fetchall():
            known_addrs.add(dict(row)["address"].lower())
    except Exception:
        pass
    try:
        for row in conn.execute(
            "SELECT address FROM whale_candidates WHERE status IN ('candidate','promoted')"
        ).fetchall():
            known_addrs.add(dict(row)["address"].lower())
    except Exception:
        pass

    deep_scan: dict[str, dict] = {}
    for addr, stats in top_addrs:
        # Smart dedup: skip addresses deep-scanned within the last 10 iterations
        last_scanned_iter = _scan_deep_history.get(addr, 0)
        if last_scanned_iter > 0 and (_scan_iteration - last_scanned_iter) < 10:
            continue
        sc = dict(stats)
        sc["markets"] = list(sc["markets"])[:15]
        # Preserve original source (leaderboard, market_scan, etc.)
        if "source" not in sc or not sc["source"]:
            sc["source"] = "trade_discovery"
        deep_scan[addr] = sc
    for ka in known_addrs:
        if ka not in deep_scan:
            # Also skip known wallets scanned recently
            last_scanned_iter = _scan_deep_history.get(ka, 0)
            if last_scanned_iter > 0 and (_scan_iteration - last_scanned_iter) < 10:
                continue
            deep_scan[ka] = {
                "total_volume": 0, "trade_count": 0,
                "markets": [], "biggest_trade": 0,
                "source": "known_wallet", "name": "",
            }
    if len(deep_scan) > 150:
        items = sorted(deep_scan.items(), key=lambda x: x[1].get("total_volume", 0), reverse=True)
        deep_scan = dict(items[:150])

    result["phase_timings"]["address_ranking"] = round(time.time() - p3_start, 2)

    # ─── Phase 4: Deep Wallet Analysis ───────────────────────────
    p4_start = time.time()
    _scan_status_text = "analyzing wallets"
    data_client = DataAPIClient()
    candidates_to_save: list[dict] = []

    addr_list = list(deep_scan.items())
    for batch_start in range(0, len(addr_list), 5):
        if _liquid_scan_stop.is_set():
            result["status"] = "stopped"
            break
        batch = addr_list[batch_start:batch_start + 5]

        async def _analyze(addr: str, ti: dict) -> dict | None:
            try:
                positions = await data_client.get_positions(addr, sort_by="CURRENT", limit=200)
                if not positions:
                    return None

                total_pnl = sum(p.cash_pnl for p in positions)
                total_invested = sum(p.initial_value for p in positions if p.initial_value > 0)
                winners = sum(1 for p in positions if p.cash_pnl > 0)
                total_count = len(positions)
                win_rate = winners / total_count if total_count > 0 else 0
                avg_pos_size = total_invested / total_count if total_count > 0 else 0

                liquid_positions = [p for p in positions if p.market_slug in liquid_slugs]
                liquid_count = len(liquid_positions)
                liquid_pct = liquid_count / total_count if total_count > 0 else 0
                liquid_value = sum(p.current_value for p in liquid_positions)

                top_mkts = sorted(positions, key=lambda p: p.current_value, reverse=True)[:5]
                top_markets_info = [
                    {"slug": p.market_slug, "title": p.title[:50],
                     "value": round(p.current_value, 2), "pnl": round(p.cash_pnl, 2)}
                    for p in top_mkts
                ]

                pnl_score = min(max(total_pnl, 0) / 100_000, 25)
                wr_score = win_rate * 25
                activity_score = min(total_count / 10, 15)
                liquid_score = liquid_pct * 10
                vol_score = min(ti.get("total_volume", 0) / 500_000, 15)
                diversity_score = min(len(ti.get("markets", [])) / 5, 10)
                score = max(0, min(100, pnl_score + wr_score + activity_score + liquid_score + vol_score + diversity_score))

                if score >= 85: grade = "S"
                elif score >= 70: grade = "A"
                elif score >= 55: grade = "B"
                elif score >= 40: grade = "C"
                elif score >= 25: grade = "D"
                else: grade = "F"

                display_name = ti.get("name", "") or addr[:10]

                return {
                    "address": addr,
                    "name": display_name,
                    "total_pnl": round(total_pnl, 2),
                    "win_rate": round(win_rate, 4),
                    "active_positions": total_count,
                    "total_volume": round(max(total_invested, ti.get("total_volume", 0)), 2),
                    "avg_position_size": round(avg_pos_size, 2),
                    "liquid_market_count": liquid_count,
                    "liquid_market_pct": round(liquid_pct, 4),
                    "score": round(score, 1),
                    "grade": grade,
                    "source": ti.get("source", "trade_discovery"),
                    "top_markets_json": json.dumps(top_markets_info),
                    "scan_data_json": json.dumps({
                        "total_pnl_raw": round(total_pnl, 2),
                        "trade_volume": round(ti.get("total_volume", 0), 2),
                        "trade_count": ti.get("trade_count", 0),
                        "markets_seen": ti.get("markets", [])[:15],
                        "biggest_trade": round(ti.get("biggest_trade", 0), 2),
                        "liquid_value": round(liquid_value, 2),
                        "winners": winners,
                        "losers": total_count - winners,
                    }),
                }
            except Exception as e:
                result["errors"].append(f"{addr[:10]}: {str(e)[:60]}")
                return None

        tasks = [_analyze(a, i) for a, i in batch]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        for idx, br in enumerate(batch_results):
            # Record that this address was deep-scanned this iteration
            if idx < len(batch):
                _scan_deep_history[batch[idx][0]] = _scan_iteration
            if isinstance(br, dict):
                result["wallets_scanned"] += 1
                candidates_to_save.append(br)
            elif isinstance(br, Exception):
                result["errors"].append(str(br)[:80])
        await asyncio.sleep(0.4)

    await data_client.close()
    result["phase_timings"]["wallet_analysis"] = round(time.time() - p4_start, 2)

    # ─── Phase 5: Score, Filter & Save ───────────────────────────
    p5_start = time.time()
    _scan_status_text = "scoring & saving"
    quality = [
        c for c in candidates_to_save
        if c["win_rate"] >= min_win_rate
        and c["total_pnl"] >= min_pnl
        and c["active_positions"] >= min_positions
    ]
    quality.sort(key=lambda c: c["score"], reverse=True)
    quality = quality[:max_candidates]
    result["candidates_found"] = len(quality)

    for c in quality:
        try:
            conn.execute(
                """INSERT INTO whale_candidates
                   (address, name, total_pnl, win_rate, active_positions,
                    total_volume, avg_position_size, liquid_market_count,
                    liquid_market_pct, score, grade, source,
                    top_markets_json, scan_data_json, last_scanned_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(address) DO UPDATE SET
                    name = excluded.name,
                    total_pnl = excluded.total_pnl,
                    win_rate = excluded.win_rate,
                    active_positions = excluded.active_positions,
                    total_volume = excluded.total_volume,
                    avg_position_size = excluded.avg_position_size,
                    liquid_market_count = excluded.liquid_market_count,
                    liquid_market_pct = excluded.liquid_market_pct,
                    score = excluded.score,
                    grade = excluded.grade,
                    top_markets_json = excluded.top_markets_json,
                    scan_data_json = excluded.scan_data_json,
                    last_scanned_at = datetime('now')""",
                (c["address"], c["name"], c["total_pnl"], c["win_rate"],
                 c["active_positions"], c["total_volume"], c["avg_position_size"],
                 c["liquid_market_count"], c["liquid_market_pct"],
                 c["score"], c["grade"], c["source"],
                 c["top_markets_json"], c["scan_data_json"]),
            )
        except Exception:
            pass

    duration = round(time.time() - iter_start, 2)
    result["phase_timings"]["scoring"] = round(time.time() - p5_start, 2)
    result["status"] = "complete"
    result["duration_s"] = duration

    conn.execute(
        """UPDATE whale_scan_config SET
           last_scan_at = ?, last_scan_status = 'scanning',
           last_scan_markets = ?, last_scan_wallets = ?,
           last_scan_candidates = ?, last_scan_duration_s = ?,
           last_scan_trades_analyzed = ?, last_scan_addresses_discovered = ?,
           last_scan_error = '', total_scans = total_scans + 1
           WHERE id = 1""",
        (now_iso, len(all_liquid_markets) or cfg.get("last_scan_markets", 0),
         result["wallets_scanned"], result["candidates_found"], duration,
         _scan_total_trades, _scan_total_unique),
    )
    conn.commit()
    conn.close()
    _scan_status_text = "idle"
    return result


def _liquid_scan_loop():
    """Background thread — runs continuously with no idle waits."""
    import asyncio
    global _scan_address_stats, _scan_total_trades, _scan_total_unique
    global _scan_iteration, _scan_last_offset, _scan_status_text
    global _scan_deep_history, _scan_leaderboard_seeded, _scan_market_trades
    global _scanner_api_pool

    # Reset accumulators on fresh start
    _scan_address_stats = {}
    _scan_total_trades = 0
    _scan_total_unique = 0
    _scan_iteration = 0
    _scan_last_offset = 0
    _scan_status_text = "starting"
    _scan_deep_history = {}
    _scan_leaderboard_seeded = 0
    _scan_market_trades = 0

    # (Re-)initialize the API pool on each scanner start
    _scanner_api_pool = load_pool_from_config()

    while not _liquid_scan_stop.is_set():
        try:
            conn = _get_conn()
            _ensure_tables(conn)
            cfg = dict(conn.execute("SELECT * FROM whale_scan_config WHERE id = 1").fetchone() or {})
            conn.close()
            if not cfg.get("enabled", 0):
                break
        except Exception:
            pass

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_run_continuous_scan_iteration())
            loop.close()
        except Exception as e:
            try:
                conn2 = _get_conn()
                conn2.execute(
                    "UPDATE whale_scan_config SET last_scan_status = 'error', last_scan_error = ? WHERE id = 1",
                    (str(e)[:200],),
                )
                conn2.commit()
                conn2.close()
            except Exception:
                pass

        # Brief pause between iterations (2s) — NOT the old interval wait
        _liquid_scan_stop.wait(timeout=2)


@app.route("/api/whales/liquid-scan/status")
def api_liquid_scan_status() -> Any:
    """Get scanner status + candidates."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        cfg = dict(conn.execute("SELECT * FROM whale_scan_config WHERE id = 1").fetchone() or {})
        cand_rows = conn.execute(
            "SELECT * FROM whale_candidates WHERE status = 'candidate' ORDER BY score DESC LIMIT 50"
        ).fetchall()
        candidates = []
        for cr in cand_rows:
            cd = dict(cr)
            try:
                cd["top_markets"] = json.loads(cd.pop("top_markets_json", "[]"))
            except (json.JSONDecodeError, TypeError):
                cd["top_markets"] = []
            try:
                cd["scan_data"] = json.loads(cd.pop("scan_data_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                cd["scan_data"] = {}
            candidates.append(cd)

        promoted_rows = conn.execute(
            "SELECT * FROM whale_candidates WHERE status = 'promoted' ORDER BY promoted_at DESC LIMIT 20"
        ).fetchall()
        promoted = []
        for pr in promoted_rows:
            pd = dict(pr)
            try:
                pd["top_markets"] = json.loads(pd.pop("top_markets_json", "[]"))
            except (json.JSONDecodeError, TypeError):
                pd["top_markets"] = []
            pd.pop("scan_data_json", None)
            promoted.append(pd)

        # Grade distribution
        grade_dist: dict = {}
        for c in candidates:
            g = c.get("grade", "F")
            grade_dist[g] = grade_dist.get(g, 0) + 1

        # Source distribution
        source_dist: dict = {}
        for c in candidates:
            s = c.get("source", "scan")
            source_dist[s] = source_dist.get(s, 0) + 1

        # Avg stats
        if candidates:
            avg_score = round(sum(c.get("score", 0) for c in candidates) / len(candidates), 1)
            avg_wr = round(sum(c.get("win_rate", 0) for c in candidates) / len(candidates), 4)
            avg_pnl = round(sum(c.get("total_pnl", 0) for c in candidates) / len(candidates), 2)
            top_candidate = candidates[0] if candidates else None
        else:
            avg_score = avg_wr = avg_pnl = 0
            top_candidate = None

        # Scanned markets from last run
        try:
            scanned_markets = json.loads(cfg.get("last_scan_markets_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            scanned_markets = []

        return jsonify({
            "config": {
                "enabled": bool(cfg.get("enabled", 0)),
                "interval_minutes": cfg.get("interval_minutes", 15),
                "min_volume": cfg.get("min_volume", 50000),
                "min_liquidity": cfg.get("min_liquidity", 10000),
                "min_win_rate": cfg.get("min_win_rate", 0.45),
                "min_pnl": cfg.get("min_pnl", 5000),
                "min_positions": cfg.get("min_positions", 5),
            },
            "scan_status": {
                "last_scan_at": cfg.get("last_scan_at"),
                "last_scan_status": _scan_status_text if (_liquid_scan_thread is not None and _liquid_scan_thread.is_alive()) else cfg.get("last_scan_status", "idle"),
                "last_scan_markets": cfg.get("last_scan_markets", 0),
                "last_scan_wallets": cfg.get("last_scan_wallets", 0),
                "last_scan_candidates": cfg.get("last_scan_candidates", 0),
                "last_scan_duration_s": cfg.get("last_scan_duration_s", 0),
                "last_scan_trades_analyzed": cfg.get("last_scan_trades_analyzed", 0),
                "last_scan_addresses_discovered": cfg.get("last_scan_addresses_discovered", 0),
                "last_scan_error": cfg.get("last_scan_error", ""),
                "total_scans": cfg.get("total_scans", 0),
                "is_running": _liquid_scan_thread is not None and _liquid_scan_thread.is_alive(),
                "scanned_markets": scanned_markets,
                # v3 continuous accumulators
                "continuous_total_trades": _scan_total_trades,
                "continuous_unique_addresses": _scan_total_unique,
                "continuous_iteration": _scan_iteration,
                "continuous_status_text": _scan_status_text,
                # v4 discovery source accumulators
                "leaderboard_wallets_seeded": _scan_leaderboard_seeded,
                "market_trades_scanned": _scan_market_trades,
                "dedup_cache_size": len(_scan_deep_history),
                # API Pool stats
                "api_pool": _scanner_api_pool.stats if _scanner_api_pool else None,
            },
            "candidates": candidates,
            "promoted": promoted,
            "stats": {
                "total_candidates": len(candidates),
                "total_promoted": len(promoted),
                "grade_distribution": grade_dist,
                "source_distribution": source_dist,
                "avg_score": avg_score,
                "avg_win_rate": avg_wr,
                "avg_pnl": avg_pnl,
                "top_candidate": {
                    "name": top_candidate.get("name", ""),
                    "score": top_candidate.get("score", 0),
                    "grade": top_candidate.get("grade", "F"),
                } if top_candidate else None,
            },
        })
    finally:
        conn.close()


@app.route("/api/whales/liquid-scan/start", methods=["POST"])
def api_liquid_scan_start() -> Any:
    """Start or configure the auto-scanner."""
    global _liquid_scan_thread
    data = request.get_json(force=True) if request.is_json else {}
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Update config from request
        updates: list = []
        params: list = []
        for key in ("interval_minutes", "min_volume", "min_liquidity",
                     "min_win_rate", "min_pnl", "min_positions", "max_candidates"):
            if key in data:
                updates.append(f"{key} = ?")
                params.append(data[key])
        updates.append("enabled = 1")
        conn.execute(
            f"UPDATE whale_scan_config SET {', '.join(updates)} WHERE id = 1",
            params,
        )
        conn.commit()

        # Start background thread if not already running
        if _liquid_scan_thread is None or not _liquid_scan_thread.is_alive():
            _liquid_scan_stop.clear()
            _liquid_scan_thread = threading.Thread(
                target=_liquid_scan_loop, daemon=True, name="liquid-scan",
            )
            _liquid_scan_thread.start()

        return jsonify({"started": True, "message": "Liquid market scanner started"})
    finally:
        conn.close()


@app.route("/api/whales/liquid-scan/stop", methods=["POST"])
def api_liquid_scan_stop() -> Any:
    """Stop the auto-scanner."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("UPDATE whale_scan_config SET enabled = 0 WHERE id = 1")
        conn.commit()
        _liquid_scan_stop.set()
        return jsonify({"stopped": True, "message": "Scanner stopped"})
    finally:
        conn.close()


@app.route("/api/whales/liquid-scan/run", methods=["POST"])
def api_liquid_scan_run() -> Any:
    """Run a one-shot scan iteration (blocking)."""
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_run_continuous_scan_iteration())
        loop.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "errors": [str(e)[:200]]}), 500


@app.route("/api/whales/liquid-scan/promote", methods=["POST"])
def api_liquid_scan_promote() -> Any:
    """Promote a candidate to a tracked whale."""
    data = request.get_json(force=True)
    address = data.get("address", "").lower()
    if not address:
        return jsonify({"error": "address required"}), 400

    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Get candidate info
        row = conn.execute(
            "SELECT * FROM whale_candidates WHERE address = ?", (address,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Candidate not found"}), 404
        cand = dict(row)

        # Insert into tracked_wallets (or update)
        conn.execute(
            """INSERT OR REPLACE INTO tracked_wallets
               (address, name, total_pnl, win_rate, active_positions,
                total_volume, score, last_scanned)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (address, cand.get("name", ""),
             cand.get("total_pnl", 0), cand.get("win_rate", 0),
             cand.get("active_positions", 0), cand.get("total_volume", 0),
             cand.get("score", 0)),
        )

        # Mark as promoted
        conn.execute(
            "UPDATE whale_candidates SET status = 'promoted', promoted_at = datetime('now') WHERE address = ?",
            (address,),
        )
        conn.commit()
        return jsonify({
            "promoted": True,
            "name": cand.get("name", ""),
            "message": f"🐋 {cand.get('name', address[:10])} promoted to tracked wallets!",
        })
    finally:
        conn.close()


@app.route("/api/whales/liquid-scan/dismiss", methods=["POST"])
def api_liquid_scan_dismiss() -> Any:
    """Dismiss a candidate (remove from list)."""
    data = request.get_json(force=True)
    address = data.get("address", "").lower()
    if not address:
        return jsonify({"error": "address required"}), 400
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("DELETE FROM whale_candidates WHERE address = ?", (address,))
        conn.commit()
        return jsonify({"dismissed": True})
    finally:
        conn.close()


@app.route("/api/whales/liquid-scan/config", methods=["POST"])
def api_liquid_scan_config() -> Any:
    """Update scanner configuration."""
    data = request.get_json(force=True)
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        updates: list = []
        params: list = []
        for key in ("interval_minutes", "min_volume", "min_liquidity",
                     "min_win_rate", "min_pnl", "min_positions", "max_candidates"):
            if key in data:
                updates.append(f"{key} = ?")
                params.append(data[key])
        if updates:
            conn.execute(
                f"UPDATE whale_scan_config SET {', '.join(updates)} WHERE id = 1",
                params,
            )
            conn.commit()
        return jsonify({"updated": True})
    finally:
        conn.close()


# ─── API: Configuration (Full — all sections) ─────────────────────

@app.route("/api/config")
def api_config() -> Any:
    cfg = _get_config()
    return jsonify({
        "scanning": cfg.scanning.model_dump(),
        "research": cfg.research.model_dump(),
        "forecasting": cfg.forecasting.model_dump(),
        "ensemble": cfg.ensemble.model_dump(),
        "risk": cfg.risk.model_dump(),
        "drawdown": cfg.drawdown.model_dump(),
        "portfolio": cfg.portfolio.model_dump(),
        "timeline": cfg.timeline.model_dump(),
        "microstructure": cfg.microstructure.model_dump(),
        "execution": cfg.execution.model_dump(),
        "cache": cfg.cache.model_dump(),
        "engine": cfg.engine.model_dump(),
        "alerts": cfg.alerts.model_dump(),
        "observability": cfg.observability.model_dump(),
    })


# ─── API: Save Configuration ──────────────────────────────────────

_CONFIG_PATH: Path = _PROJECT_ROOT / "config.yaml"

@app.route("/api/config", methods=["POST"])
def api_config_save() -> Any:
    """Validate and save config changes to config.yaml, then hot-reload."""
    global _config
    data = request.get_json(force=True)
    if not data or not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Invalid JSON body"}), 400

    try:
        # Load current config as dict, merge changes
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH) as f:
                current_raw: dict[str, Any] = yaml.safe_load(f) or {}
        else:
            current_raw = {}

        # Deep-merge incoming sections into current raw config
        for section, values in data.items():
            if isinstance(values, dict):
                if section not in current_raw:
                    current_raw[section] = {}
                current_raw[section].update(values)
            else:
                current_raw[section] = values

        # Validate with Pydantic (raises if invalid)
        new_config = BotConfig(**current_raw)

        # Write to YAML
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CONFIG_PATH, "w") as f:
            yaml.dump(
                current_raw,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )

        # Hot-reload in memory
        _config = new_config

        return jsonify({
            "ok": True,
            "message": "Configuration saved and reloaded",
            "sections_updated": list(data.keys()),
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 422


# ─── API: Reset Configuration ─────────────────────────────────────

@app.route("/api/config/reset", methods=["POST"])
def api_config_reset() -> Any:
    """Reset config to defaults (delete config.yaml), reload."""
    global _config
    try:
        if _CONFIG_PATH.exists():
            _CONFIG_PATH.unlink()
        _config = BotConfig()
        return jsonify({"ok": True, "message": "Configuration reset to defaults"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ─── API: Performance Analytics ────────────────────────────────────

@app.route("/api/analytics")
def api_analytics() -> Any:
    """Return comprehensive performance analytics snapshot."""
    from src.analytics.performance_tracker import PerformanceTracker
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        cfg = _get_config()
        tracker = PerformanceTracker(bankroll=cfg.risk.bankroll)
        snapshot = tracker.compute(conn)
        return jsonify(snapshot.to_dict())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/regime")
def api_regime() -> Any:
    """Return current market regime and history."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # Latest regime
        current = None
        try:
            row = conn.execute("""
                SELECT regime, confidence, kelly_multiplier, size_multiplier,
                       explanation, detected_at
                FROM regime_history
                ORDER BY detected_at DESC LIMIT 1
            """).fetchone()
            if row:
                current = dict(row)
        except sqlite3.OperationalError:
            pass

        # Regime history (last 50)
        history = []
        try:
            rows = conn.execute("""
                SELECT regime, confidence, kelly_multiplier, size_multiplier,
                       explanation, detected_at
                FROM regime_history
                ORDER BY detected_at DESC LIMIT 50
            """).fetchall()
            history = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass

        return jsonify({
            "current": current,
            "history": history,
        })
    finally:
        conn.close()


@app.route("/api/calibration-curve")
def api_calibration_curve() -> Any:
    """Return calibration data for plotting."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = []
        try:
            rows = conn.execute("""
                SELECT forecast_prob, actual_outcome
                FROM calibration_history
                ORDER BY recorded_at ASC
            """).fetchall()
        except sqlite3.OperationalError:
            pass

        if not rows:
            return jsonify({"bins": [], "counts": 0})

        # Build calibration bins (0.0-0.1, 0.1-0.2, ... 0.9-1.0)
        num_bins = 10
        bin_forecasts: dict[int, list[float]] = {i: [] for i in range(num_bins)}
        bin_outcomes: dict[int, list[float]] = {i: [] for i in range(num_bins)}

        for r in rows:
            fp = float(r["forecast_prob"])
            ao = float(r["actual_outcome"])
            b = min(int(fp * num_bins), num_bins - 1)
            bin_forecasts[b].append(fp)
            bin_outcomes[b].append(ao)

        bins = []
        for i in range(num_bins):
            if bin_forecasts[i]:
                bins.append({
                    "range": f"{i / num_bins:.1f}-{(i + 1) / num_bins:.1f}",
                    "midpoint": (i + 0.5) / num_bins,
                    "avg_forecast": sum(bin_forecasts[i]) / len(bin_forecasts[i]),
                    "avg_outcome": sum(bin_outcomes[i]) / len(bin_outcomes[i]),
                    "count": len(bin_forecasts[i]),
                })

        return jsonify({
            "bins": bins,
            "total_samples": len(rows),
        })
    finally:
        conn.close()


@app.route("/api/model-accuracy")
def api_model_accuracy() -> Any:
    """Return per-model accuracy breakdown."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = []
        try:
            rows = conn.execute("""
                SELECT model_name,
                       COUNT(*) as total_forecasts,
                       AVG(ABS(forecast_prob - actual_outcome)) as avg_error,
                       AVG((forecast_prob - actual_outcome) *
                           (forecast_prob - actual_outcome)) as brier_score
                FROM model_forecast_log
                WHERE actual_outcome >= 0
                GROUP BY model_name
                ORDER BY brier_score ASC
            """).fetchall()
        except sqlite3.OperationalError:
            pass

        models = [
            {
                "model_name": r["model_name"],
                "total_forecasts": int(r["total_forecasts"]),
                "avg_error": round(float(r["avg_error"] or 0), 4),
                "brier_score": round(float(r["brier_score"] or 0), 4),
            }
            for r in rows
        ]

        # Per-category breakdown
        cat_rows = []
        try:
            cat_rows = conn.execute("""
                SELECT model_name, category,
                       COUNT(*) as cnt,
                       AVG((forecast_prob - actual_outcome) *
                           (forecast_prob - actual_outcome)) as brier
                FROM model_forecast_log
                WHERE actual_outcome >= 0
                GROUP BY model_name, category
                ORDER BY model_name, brier ASC
            """).fetchall()
        except sqlite3.OperationalError:
            pass

        by_category = [
            {
                "model_name": r["model_name"],
                "category": r["category"],
                "forecasts": int(r["cnt"]),
                "brier_score": round(float(r["brier"] or 0), 4),
            }
            for r in cat_rows
        ]

        return jsonify({
            "models": models,
            "by_category": by_category,
        })
    finally:
        conn.close()


@app.route("/api/adaptive-weights")
def api_adaptive_weights() -> Any:
    """Return current adaptive model weights per category."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        from src.analytics.adaptive_weights import AdaptiveModelWeighter
        cfg = _get_config()
        weighter = AdaptiveModelWeighter(cfg.ensemble)
        all_weights = weighter.get_all_category_weights(conn)
        return jsonify({
            cat: result.to_dict()
            for cat, result in all_weights.items()
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/admin")
def api_admin() -> Any:
    """Comprehensive admin panel data: system stats, DB stats, API usage,
    engine health, environment info, log tail, cost tracking, and more."""
    import platform
    import resource
    import subprocess
    import threading as _threading

    cfg = _get_config()
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        # ── System Information ────────────────────────────────────
        process_uptime = round(time.time() - _engine_started_at, 0) if _engine_started_at else 0

        # Memory & resource usage (cross-platform via resource module)
        try:
            rusage = resource.getrusage(resource.RUSAGE_SELF)
            mem_rss_mb = round(rusage.ru_maxrss / (1024 * 1024), 2) if platform.system() == "Linux" else round(rusage.ru_maxrss / (1024 * 1024), 2)
            # macOS reports in bytes, Linux in KB
            if platform.system() == "Darwin":
                mem_rss_mb = round(rusage.ru_maxrss / (1024 * 1024), 2)
            else:
                mem_rss_mb = round(rusage.ru_maxrss / 1024, 2)
        except Exception:
            mem_rss_mb = 0

        thread_count = _threading.active_count()
        pid = os.getpid()

        # Open file descriptors
        try:
            fd_count = len(os.listdir(f"/proc/{pid}/fd"))
        except Exception:
            try:
                result = subprocess.run(
                    ["lsof", "-p", str(pid)], capture_output=True, text=True, timeout=5
                )
                fd_count = max(0, len(result.stdout.strip().split("\n")) - 1) if result.stdout else 0
            except Exception:
                fd_count = 0

        system_info = {
            "hostname": platform.node(),
            "platform": f"{platform.system()} {platform.release()}",
            "python_version": platform.python_version(),
            "architecture": platform.machine(),
            "process_uptime_secs": process_uptime,
            "process_uptime_human": _format_duration(process_uptime),
            "pid": pid,
            "memory_rss_mb": mem_rss_mb,
            "thread_count": thread_count,
            "cpu_count": os.cpu_count() or 1,
            "open_fds": fd_count,
        }

        # ── Database Statistics ───────────────────────────────────
        db_file = Path(_db_path)
        db_size_bytes = db_file.stat().st_size if db_file.exists() else 0
        db_size_mb = round(db_size_bytes / (1024 * 1024), 2)

        table_stats = {}
        for tbl in ["markets", "forecasts", "trades", "positions", "candidates",
                     "alerts_log", "engine_state", "regime_history",
                     "calibration_history", "model_forecast_log",
                     "audit_trail", "tracked_wallets", "wallet_signals",
                     "wallet_deltas", "fill_records", "performance_log"]:
            try:
                row = conn.execute(f"SELECT COUNT(*) as cnt FROM {tbl}").fetchone()
                table_stats[tbl] = int(row["cnt"]) if row else 0
            except Exception:
                table_stats[tbl] = -1  # table doesn't exist

        total_rows = sum(v for v in table_stats.values() if v >= 0)

        # Oldest and newest records
        db_age_info = {}
        for tbl in ["trades", "forecasts", "positions"]:
            try:
                oldest = conn.execute(f"SELECT MIN(created_at) as ts FROM {tbl}").fetchone()
                newest = conn.execute(f"SELECT MAX(created_at) as ts FROM {tbl}").fetchone()
                db_age_info[tbl] = {
                    "oldest": oldest["ts"] if oldest else None,
                    "newest": newest["ts"] if newest else None,
                }
            except Exception:
                db_age_info[tbl] = {"oldest": None, "newest": None}

        # SQLite pragma info
        try:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
            page_size = conn.execute("PRAGMA page_size").fetchone()
            page_count = conn.execute("PRAGMA page_count").fetchone()
            freelist_count = conn.execute("PRAGMA freelist_count").fetchone()
            db_pragma = {
                "journal_mode": journal_mode[0] if journal_mode else "unknown",
                "page_size": page_size[0] if page_size else 0,
                "page_count": page_count[0] if page_count else 0,
                "freelist_pages": freelist_count[0] if freelist_count else 0,
                "fragmentation_pct": round(
                    (freelist_count[0] / max(page_count[0], 1)) * 100, 1
                ) if freelist_count and page_count else 0,
            }
        except Exception:
            db_pragma = {}

        db_stats = {
            "path": str(db_file.resolve()),
            "size_bytes": db_size_bytes,
            "size_mb": db_size_mb,
            "total_rows": total_rows,
            "table_stats": table_stats,
            "age_info": db_age_info,
            "pragma": db_pragma,
        }

        # ── API Key / Credentials Status ──────────────────────────
        env_keys = {
            "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "GOOGLE_API_KEY": bool(os.environ.get("GOOGLE_API_KEY")),
            "POLYMARKET_API_KEY": bool(os.environ.get("POLYMARKET_API_KEY")),
            "POLYMARKET_SECRET": bool(os.environ.get("POLYMARKET_SECRET")),
            "POLYMARKET_PASSPHRASE": bool(os.environ.get("POLYMARKET_PASSPHRASE")),
            "CLOB_API_KEY": bool(os.environ.get("CLOB_API_KEY")),
            "PRIVATE_KEY": bool(os.environ.get("PRIVATE_KEY")),
            "ENABLE_LIVE_TRADING": os.environ.get("ENABLE_LIVE_TRADING", "false"),
            "DASHBOARD_API_KEY": bool(os.environ.get("DASHBOARD_API_KEY")),
            "SENTRY_DSN": bool(os.environ.get("SENTRY_DSN")),
            "TELEGRAM_BOT_TOKEN": bool(os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.alerts.telegram_bot_token),
            "DISCORD_WEBHOOK_URL": bool(os.environ.get("DISCORD_WEBHOOK_URL") or cfg.alerts.discord_webhook_url),
            "SLACK_WEBHOOK_URL": bool(os.environ.get("SLACK_WEBHOOK_URL") or cfg.alerts.slack_webhook_url),
            "SERPER_API_KEY": bool(os.environ.get("SERPER_API_KEY")),
            "TAVILY_API_KEY": bool(os.environ.get("TAVILY_API_KEY")),
        }
        keys_configured = sum(1 for v in env_keys.values() if v and v is not False and v != "false")
        keys_total = len(env_keys)

        # ── LLM / API Cost Tracking ──────────────────────────────
        snap = metrics.snapshot()
        counters = snap.get("counters", {})
        gauges = snap.get("gauges", {})
        histograms = snap.get("histograms", {})

        # Estimate costs from metrics
        llm_calls = counters.get("llm.calls", 0)
        llm_input_tokens = counters.get("llm.input_tokens", 0)
        llm_output_tokens = counters.get("llm.output_tokens", 0)
        search_calls = counters.get("search.calls", 0)
        api_errors = counters.get("api.errors", 0)

        # Rough cost estimates (per 1M tokens)
        cost_per_1m_input = {"gpt-4o": 2.50, "claude-3-5-sonnet-20241022": 3.00, "gemini-1.5-pro": 1.25}
        cost_per_1m_output = {"gpt-4o": 10.00, "claude-3-5-sonnet-20241022": 15.00, "gemini-1.5-pro": 5.00}
        model = cfg.forecasting.llm_model
        input_cost_rate = cost_per_1m_input.get(model, 2.50)
        output_cost_rate = cost_per_1m_output.get(model, 10.00)
        est_llm_cost = (llm_input_tokens * input_cost_rate / 1_000_000) + (llm_output_tokens * output_cost_rate / 1_000_000)
        est_search_cost = search_calls * 0.001  # rough estimate

        # Cost from CostTracker
        from src.observability.metrics import cost_tracker
        ct_snap = cost_tracker.snapshot()

        cost_tracking = {
            "llm_calls": llm_calls,
            "llm_input_tokens": llm_input_tokens,
            "llm_output_tokens": llm_output_tokens,
            "llm_total_tokens": llm_input_tokens + llm_output_tokens,
            "search_calls": search_calls,
            "api_errors": api_errors,
            "estimated_llm_cost_usd": round(est_llm_cost, 4),
            "estimated_search_cost_usd": round(est_search_cost, 4),
            "estimated_total_cost_usd": round(est_llm_cost + est_search_cost, 4),
            "cost_tracker_total_usd": ct_snap.get("total_cost_usd", 0),
            "cost_tracker_cycle_usd": ct_snap.get("cycle_cost_usd", 0),
            "cost_tracker_calls": ct_snap.get("total_calls", {}),
            "primary_model": model,
            "ensemble_models": cfg.ensemble.models if cfg.ensemble.enabled else [model],
            "ensemble_enabled": cfg.ensemble.enabled,
        }

        # ── Rate Limiter Stats ────────────────────────────────────
        from src.connectors.rate_limiter import rate_limiter as _rl
        rate_limiter_stats = _rl.stats()

        # ── Engine Health ─────────────────────────────────────────
        engine_running = (
            _engine_instance is not None
            and _engine_instance.is_running
            and _engine_thread is not None
            and _engine_thread.is_alive()
        )
        engine_health = {
            "running": engine_running,
            "thread_alive": _engine_thread.is_alive() if _engine_thread else False,
            "error": _engine_error,
            "cycle_count": _engine_instance._cycle_count if _engine_instance else 0,
            "started_at": _engine_started_at,
            "uptime_secs": round(time.time() - _engine_started_at, 0) if _engine_started_at else 0,
            "paper_mode": cfg.engine.paper_mode,
            "live_trading": is_live_trading_enabled(),
            "cycle_interval_secs": cfg.engine.cycle_interval_secs,
            "auto_start": cfg.engine.auto_start,
        }

        # ── Cycle Performance History ─────────────────────────────
        cycle_history = []
        if _engine_instance and hasattr(_engine_instance, "_cycle_history"):
            for ch in _engine_instance._cycle_history[-50:]:
                cycle_history.append({
                    "cycle_id": ch.cycle_id,
                    "duration_secs": ch.duration_secs,
                    "markets_scanned": ch.markets_scanned,
                    "markets_researched": ch.markets_researched,
                    "edges_found": ch.edges_found,
                    "trades_executed": ch.trades_executed,
                    "status": ch.status,
                    "errors": len(ch.errors) if ch.errors else 0,
                })

        # ── Metrics Snapshot ──────────────────────────────────────
        all_counters = {k: v for k, v in counters.items()}
        all_gauges = {k: v for k, v in gauges.items()}

        # ── Trading Summary ───────────────────────────────────────
        today = dt.date.today().isoformat()
        total_trades_row = conn.execute("SELECT COUNT(*) as cnt FROM trades").fetchone()
        total_forecasts_row = conn.execute("SELECT COUNT(*) as cnt FROM forecasts").fetchone()
        today_trades_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE date(created_at) = ?", (today,)
        ).fetchone()
        today_forecasts_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM forecasts WHERE date(created_at) = ?", (today,)
        ).fetchone()

        # Trade decisions breakdown
        trade_decisions_row = conn.execute(
            "SELECT decision, COUNT(*) as cnt FROM forecasts GROUP BY decision"
        ).fetchall()
        decision_breakdown = {r["decision"]: r["cnt"] for r in trade_decisions_row}

        # Avg processing per cycle
        avg_candidates = 0
        try:
            avg_row = conn.execute(
                "SELECT AVG(cnt) as avg_cnt FROM (SELECT cycle_id, COUNT(*) as cnt FROM candidates GROUP BY cycle_id)"
            ).fetchone()
            avg_candidates = round(float(avg_row["avg_cnt"] or 0), 1) if avg_row else 0
        except Exception:
            pass

        # Win/loss from positions
        try:
            win_row = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE pnl > 0").fetchone()
            loss_row = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE pnl < 0").fetchone()
            neutral_row = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE pnl = 0 OR pnl IS NULL").fetchone()
            win_count = int(win_row["cnt"]) if win_row else 0
            loss_count = int(loss_row["cnt"]) if loss_row else 0
            neutral_count = int(neutral_row["cnt"]) if neutral_row else 0
        except Exception:
            win_count = loss_count = neutral_count = 0

        # Hourly activity (last 24h trade counts by hour)
        hourly_activity = []
        try:
            hourly_rows = conn.execute("""
                SELECT strftime('%%H', created_at) as hour, COUNT(*) as cnt
                FROM forecasts
                WHERE created_at >= datetime('now', '-24 hours')
                GROUP BY hour ORDER BY hour
            """).fetchall()
            hourly_activity = [{"hour": r["hour"], "count": r["cnt"]} for r in hourly_rows]
        except Exception:
            pass

        trading_summary = {
            "total_trades": int(total_trades_row["cnt"]) if total_trades_row else 0,
            "total_forecasts": int(total_forecasts_row["cnt"]) if total_forecasts_row else 0,
            "today_trades": int(today_trades_row["cnt"]) if today_trades_row else 0,
            "today_forecasts": int(today_forecasts_row["cnt"]) if today_forecasts_row else 0,
            "decision_breakdown": decision_breakdown,
            "avg_candidates_per_cycle": avg_candidates,
            "win_count": win_count,
            "loss_count": loss_count,
            "neutral_count": neutral_count,
            "hourly_activity": hourly_activity,
        }

        # ── Log File Info ─────────────────────────────────────────
        log_path = Path(cfg.observability.log_file)
        log_info = {"path": str(log_path), "exists": log_path.exists(), "size_mb": 0, "lines": 0, "recent_errors": [], "error_count": 0, "warn_count": 0}
        if log_path.exists():
            log_info["size_mb"] = round(log_path.stat().st_size / (1024 * 1024), 2)
            try:
                result = subprocess.run(
                    ["wc", "-l", str(log_path)], capture_output=True, text=True, timeout=5
                )
                log_info["lines"] = int(result.stdout.strip().split()[0]) if result.stdout else 0
            except Exception:
                pass
            # Count errors and warnings
            try:
                result = subprocess.run(
                    ["grep", "-ic", "error\\|exception\\|traceback", str(log_path)],
                    capture_output=True, text=True, timeout=5
                )
                log_info["error_count"] = int(result.stdout.strip()) if result.stdout.strip() else 0
            except Exception:
                pass
            try:
                result = subprocess.run(
                    ["grep", "-ic", "warn", str(log_path)],
                    capture_output=True, text=True, timeout=5
                )
                log_info["warn_count"] = int(result.stdout.strip()) if result.stdout.strip() else 0
            except Exception:
                pass
            # Recent error lines (last 20 errors)
            try:
                result = subprocess.run(
                    ["grep", "-i", "error\\|exception\\|traceback", str(log_path)],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout:
                    lines = result.stdout.strip().split("\n")
                    log_info["recent_errors"] = lines[-20:]
            except Exception:
                pass

        # ── Config File Info ──────────────────────────────────────
        config_path = _PROJECT_ROOT / "config.yaml"
        config_info = {
            "path": str(config_path),
            "exists": config_path.exists(),
            "size_bytes": config_path.stat().st_size if config_path.exists() else 0,
            "last_modified": dt.datetime.fromtimestamp(config_path.stat().st_mtime).isoformat() if config_path.exists() else None,
        }

        # ── Storage / Backup Info ─────────────────────────────────
        data_dir = Path("data")
        backup_files = []
        if data_dir.exists():
            for f in sorted(data_dir.glob("*.db.bak*"), reverse=True)[:10]:
                backup_files.append({
                    "name": f.name,
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
                    "modified": dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                })

        # Data directory breakdown
        dir_breakdown = {}
        for dir_name in ["data", "logs", "reports"]:
            d = Path(dir_name)
            if d.exists():
                total = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                file_count = sum(1 for f in d.rglob("*") if f.is_file())
                dir_breakdown[dir_name] = {
                    "size_mb": round(total / (1024 * 1024), 2),
                    "file_count": file_count,
                }
            else:
                dir_breakdown[dir_name] = {"size_mb": 0, "file_count": 0}

        # ── Feature Flags / Config Summary ────────────────────────
        feature_flags = {
            "ensemble_enabled": cfg.ensemble.enabled,
            "drawdown_enabled": cfg.drawdown.enabled,
            "wallet_scanner_enabled": cfg.wallet_scanner.enabled,
            "alerts_enabled": cfg.alerts.enabled,
            "cache_enabled": cfg.cache.enabled,
            "twap_enabled": cfg.execution.twap_enabled,
            "adaptive_pricing": cfg.execution.adaptive_pricing,
            "dry_run": cfg.execution.dry_run,
            "kill_switch": cfg.risk.kill_switch,
            "paper_mode": cfg.engine.paper_mode,
            "live_trading": is_live_trading_enabled(),
            "daily_summary": cfg.alerts.daily_summary_enabled,
            "metrics_enabled": cfg.observability.enable_metrics,
        }

        # ── Health Score Calculation ──────────────────────────────
        health_score = 100
        health_issues = []

        # Engine running?
        if not engine_running:
            health_score -= 30
            health_issues.append({"severity": "critical", "message": "Engine is not running"})
        # Thread alive?
        if _engine_thread and not _engine_thread.is_alive():
            health_score -= 20
            health_issues.append({"severity": "critical", "message": "Engine thread is dead"})
        # Engine error?
        if _engine_error:
            health_score -= 15
            health_issues.append({"severity": "error", "message": f"Engine error: {_engine_error[:100]}"})
        # API keys configured?
        if keys_configured < 3:
            health_score -= 10
            health_issues.append({"severity": "warning", "message": f"Only {keys_configured}/{keys_total} API keys configured"})
        # DB fragmentation?
        if db_pragma.get("fragmentation_pct", 0) > 20:
            health_score -= 5
            health_issues.append({"severity": "info", "message": f"DB fragmentation at {db_pragma['fragmentation_pct']}% — consider VACUUM"})
        # Log file too large?
        if log_info.get("size_mb", 0) > 50:
            health_score -= 5
            health_issues.append({"severity": "warning", "message": f"Log file is {log_info['size_mb']} MB — consider rotating"})
        # Memory usage?
        if mem_rss_mb > 500:
            health_score -= 10
            health_issues.append({"severity": "warning", "message": f"High memory usage: {mem_rss_mb} MB"})
        # API errors?
        if api_errors > 50:
            health_score -= 10
            health_issues.append({"severity": "warning", "message": f"{int(api_errors)} API errors recorded"})
        # No recent activity?
        if _engine_instance and hasattr(_engine_instance, "_cycle_count") and _engine_instance._cycle_count == 0:
            health_score -= 5
            health_issues.append({"severity": "info", "message": "No cycles completed yet"})

        health_score = max(0, min(100, health_score))
        if health_score >= 90:
            health_grade = "A"
        elif health_score >= 75:
            health_grade = "B"
        elif health_score >= 60:
            health_grade = "C"
        elif health_score >= 40:
            health_grade = "D"
        else:
            health_grade = "F"

        health = {
            "score": health_score,
            "grade": health_grade,
            "issues": health_issues,
        }

        # ── Recent Alerts (last 50) ──────────────────────────────
        recent_alerts = []
        try:
            alert_rows = conn.execute(
                "SELECT level, message, created_at FROM alerts_log ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
            recent_alerts = [{"level": r["level"], "message": r["message"], "time": r["created_at"]} for r in alert_rows]
        except Exception:
            pass

        # ── Histograms ────────────────────────────────────────────
        all_histograms = {k: v for k, v in histograms.items()}

        return jsonify({
            "system_info": system_info,
            "db_stats": db_stats,
            "api_keys": env_keys,
            "keys_configured": keys_configured,
            "keys_total": keys_total,
            "cost_tracking": cost_tracking,
            "rate_limiter": rate_limiter_stats,
            "engine_health": engine_health,
            "cycle_history": cycle_history,
            "counters": all_counters,
            "gauges": all_gauges,
            "histograms": all_histograms,
            "trading_summary": trading_summary,
            "log_info": log_info,
            "config_info": config_info,
            "backup_files": backup_files,
            "dir_breakdown": dir_breakdown,
            "feature_flags": feature_flags,
            "health": health,
            "recent_alerts": recent_alerts,
        })
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/admin/log-tail")
def api_admin_log_tail() -> Any:
    """Return the last N lines of the bot log file."""
    import subprocess
    cfg = _get_config()
    n = request.args.get("lines", 100, type=int)
    log_path = Path(cfg.observability.log_file)
    if not log_path.exists():
        return jsonify({"lines": [], "total": 0})
    try:
        result = subprocess.run(
            ["tail", "-n", str(min(n, 500)), str(log_path)],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n") if result.stdout else []
        return jsonify({"lines": lines, "total": len(lines)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/db-vacuum", methods=["POST"])
def api_admin_db_vacuum() -> Any:
    """Run VACUUM on the database to reclaim space."""
    try:
        conn = _get_conn()
        conn.execute("VACUUM")
        conn.close()
        return jsonify({"ok": True, "message": "Database vacuumed successfully"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/admin/clear-cache", methods=["POST"])
def api_admin_clear_cache() -> Any:
    """Clear the in-memory cache."""
    try:
        from src.storage.cache import cache
        cache.clear()
        return jsonify({"ok": True, "message": "Cache cleared successfully"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/admin/purge-old", methods=["POST"])
def api_admin_purge_old() -> Any:
    """Purge records older than N days from large tables."""
    days = request.json.get("days", 30) if request.json else 30
    try:
        conn = _get_conn()
        _ensure_tables(conn)
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
        deleted = {}
        for tbl in ["candidates", "alerts_log", "model_forecast_log", "calibration_history"]:
            try:
                cur = conn.execute(f"DELETE FROM {tbl} WHERE created_at < ?", (cutoff,))
                deleted[tbl] = cur.rowcount
            except Exception:
                deleted[tbl] = 0
        conn.commit()
        conn.close()
        total = sum(deleted.values())
        return jsonify({"ok": True, "deleted": deleted, "total_deleted": total, "message": f"Purged {total} records older than {days} days"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/admin/rotate-logs", methods=["POST"])
def api_admin_rotate_logs() -> Any:
    """Rotate the log file (rename current and start fresh)."""
    try:
        cfg = _get_config()
        log_path = Path(cfg.observability.log_file)
        if log_path.exists():
            ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            rotated = log_path.with_suffix(f".{ts}.log")
            log_path.rename(rotated)
            log_path.touch()
            return jsonify({"ok": True, "message": f"Log rotated to {rotated.name}"})
        return jsonify({"ok": True, "message": "No log file to rotate"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/admin/backup-db", methods=["POST"])
def api_admin_backup_db() -> Any:
    """Create a backup copy of the database."""
    import shutil
    try:
        db_file = Path(_db_path)
        if not db_file.exists():
            return jsonify({"ok": False, "error": "Database file not found"})
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_file.with_suffix(f".db.bak.{ts}")
        shutil.copy2(str(db_file), str(backup_path))
        size_mb = round(backup_path.stat().st_size / (1024 * 1024), 2)
        return jsonify({"ok": True, "message": f"Backup created: {backup_path.name} ({size_mb} MB)"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/admin/reset-metrics", methods=["POST"])
def api_admin_reset_metrics() -> Any:
    """Reset all in-memory metrics counters."""
    try:
        metrics.reset()
        return jsonify({"ok": True, "message": "Metrics reset successfully"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/admin/test-alert", methods=["POST"])
def api_admin_test_alert() -> Any:
    """Send a test alert through all configured channels."""
    try:
        from src.observability.alerts import AlertManager
        cfg = _get_config()
        alert_mgr = AlertManager(cfg.alerts)
        alert_mgr.send("Test alert from Admin Panel", level="info")
        return jsonify({"ok": True, "message": "Test alert sent to all configured channels"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/admin/export/<table_name>")
def api_admin_export(table_name: str) -> Any:
    """Export a database table as JSON (for CSV download on client)."""
    allowed = {
        "forecasts", "trades", "positions", "markets", "candidates",
        "alerts_log", "audit_trail", "tracked_wallets", "wallet_signals",
    }
    if table_name not in allowed:
        return jsonify({"error": f"Table '{table_name}' not exportable"}), 404
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute(f"SELECT * FROM {table_name} ORDER BY rowid DESC LIMIT 10000").fetchall()
        data = [dict(r) for r in rows]
        return jsonify({"table": table_name, "count": len(data), "rows": data})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


def _format_duration(secs: float) -> str:
    """Format seconds into a human-readable duration string."""
    if secs <= 0:
        return "—"
    days = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    mins = int((secs % 3600) // 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


@app.route("/api/equity-curve")
def api_equity_curve() -> Any:
    """Return equity curve data for charting.

    Uses performance_log (realized P&L) when available.
    Falls back to positions table (unrealized P&L) for paper trading
    where no positions have resolved yet.  Uses the same data source
    as /api/portfolio so the final cumulative P&L always matches the
    Total P&L card on the dashboard.
    """
    from src.analytics.performance_tracker import PerformanceTracker
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        cfg = _get_config()
        bankroll = cfg.risk.bankroll

        # ── Try realized P&L from performance_log first ──────────
        tracker = PerformanceTracker(bankroll=bankroll)
        snapshot = tracker.compute(conn)
        if snapshot.equity_curve:
            return jsonify({
                "points": [e.to_dict() for e in snapshot.equity_curve],
                "bankroll": bankroll,
            })

        # ── Fallback: build from positions (unrealized P&L) ────────
        # Uses the same data source as /api/portfolio Total P&L so
        # the equity-curve final point always matches the P&L card.
        positions = conn.execute("""
            SELECT market_id, pnl, stake_usd, opened_at
            FROM positions
            ORDER BY opened_at ASC
        """).fetchall()

        if not positions:
            return jsonify({"points": [], "bankroll": bankroll})

        cum_pnl = 0.0
        peak_equity = bankroll
        points = []
        pos_num = 0

        for r in positions:
            pos_num += 1
            pos_pnl = float(r["pnl"] or 0)
            cum_pnl += pos_pnl

            equity = bankroll + cum_pnl
            peak_equity = max(peak_equity, equity)
            dd = ((peak_equity - equity) / peak_equity) if peak_equity > 0 else 0.0

            points.append({
                "timestamp": r["opened_at"],
                "equity": round(equity, 2),
                "pnl_cumulative": round(cum_pnl, 2),
                "drawdown_pct": round(dd, 4),
                "trade_count": pos_num,
            })

        return jsonify({"points": points, "bankroll": bankroll})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─── API: Watchlist ────────────────────────────────────────────────

@app.route("/api/watchlist")
def api_watchlist() -> Any:
    """Get all watchlist items."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
        return jsonify({"items": [dict(r) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add() -> Any:
    """Add a market to the watchlist."""
    data = request.json or {}
    market_id = data.get("market_id", "")
    question = data.get("question", "")
    category = data.get("category", "")
    notes = data.get("notes", "")
    if not market_id:
        return jsonify({"error": "market_id required"}), 400
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (market_id, question, category, notes, added_at) VALUES (?,?,?,?,?)",
            (market_id, question, category, notes, dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/watchlist/<market_id>", methods=["DELETE"])
def api_watchlist_remove(market_id: str) -> Any:
    """Remove a market from the watchlist."""
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("DELETE FROM watchlist WHERE market_id = ?", (market_id,))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─── API: Trade Journal ───────────────────────────────────────────

@app.route("/api/journal")
def api_journal() -> Any:
    """Get trade journal entries."""
    limit = request.args.get("limit", 100, type=int)
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute(
            "SELECT * FROM trade_journal ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return jsonify({"entries": [dict(r) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/journal", methods=["POST"])
def api_journal_add() -> Any:
    """Create or update a journal entry."""
    data = request.json or {}
    conn = _get_conn()
    _ensure_tables(conn)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        journal_id = data.get("id")
        if journal_id:
            # Update existing
            conn.execute(
                "UPDATE trade_journal SET annotation=?, lessons_learned=?, tags=?, updated_at=? WHERE id=?",
                (data.get("annotation", ""), data.get("lessons_learned", ""),
                 data.get("tags", "[]"), now, journal_id),
            )
        else:
            # Insert new
            conn.execute(
                """INSERT INTO trade_journal
                    (market_id, question, direction, entry_price, exit_price,
                     stake_usd, pnl, annotation, reasoning, lessons_learned, tags,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data.get("market_id", ""), data.get("question", ""),
                 data.get("direction", ""), data.get("entry_price", 0),
                 data.get("exit_price", 0), data.get("stake_usd", 0),
                 data.get("pnl", 0), data.get("annotation", ""),
                 data.get("reasoning", ""), data.get("lessons_learned", ""),
                 data.get("tags", "[]"), now, now),
            )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─── API: VaR ─────────────────────────────────────────────────────

@app.route("/api/var")
def api_var() -> Any:
    """Calculate current portfolio VaR and return history."""
    from src.policy.portfolio_risk import PositionSnapshot, calculate_portfolio_var
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        cfg = _get_config()
        bankroll = cfg.risk.bankroll

        positions = conn.execute(
            "SELECT market_id, stake_usd, entry_price, current_price, pnl, direction FROM positions"
        ).fetchall()
        snaps = []
        for r in positions:
            snaps.append(PositionSnapshot(
                market_id=r["market_id"],
                question="",
                category="",
                event_slug="",
                side=(r["direction"] or "YES"),
                size_usd=float(r["stake_usd"] or 0),
                entry_price=float(r["entry_price"] or 0.5),
                current_price=float(r["current_price"] or 0.5),
                unrealised_pnl=float(r["pnl"] or 0),
            ))

        current_var = calculate_portfolio_var(snaps, bankroll)

        # Save to history
        try:
            conn.execute(
                """INSERT INTO var_history (timestamp, daily_var_95, daily_var_99, portfolio_value, num_positions, method, details_json)
                VALUES (?,?,?,?,?,?,?)""",
                (dt.datetime.now(dt.timezone.utc).isoformat(),
                 current_var["daily_var_95"], current_var["daily_var_99"],
                 bankroll, len(snaps), "parametric", json.dumps(current_var.get("components", []))),
            )
            conn.commit()
        except Exception:
            pass

        # Get history
        history = conn.execute(
            "SELECT * FROM var_history ORDER BY timestamp DESC LIMIT 100"
        ).fetchall()

        return jsonify({
            "current": current_var,
            "history": [dict(r) for r in history],
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─── API: Equity Snapshots (high-res) ─────────────────────────────

@app.route("/api/equity-snapshots")
def api_equity_snapshots() -> Any:
    """Return stored equity snapshots for P&L charting."""
    limit = request.args.get("limit", 500, type=int)
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute(
            "SELECT * FROM equity_snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        data = [dict(r) for r in reversed(rows)]
        return jsonify({"snapshots": data})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─── API: Config Reload ───────────────────────────────────────────

@app.route("/api/config/reload", methods=["POST"])
def api_config_reload() -> Any:
    """Hot-reload config from disk."""
    global _config
    try:
        _config = load_config()
        return jsonify({"ok": True, "message": "Configuration reloaded from disk"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ─── API: Environment Variables (.env management) ─────────────────

_ENV_FILE = _PROJECT_ROOT / ".env"

# Keys that are safe to manage from the dashboard
_MANAGED_ENV_KEYS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE",
    "POLYMARKET_PRIVATE_KEY", "POLYMARKET_CHAIN_ID",
    "CLOB_API_KEY", "PRIVATE_KEY",
    "SERPAPI_KEY", "TAVILY_API_KEY", "SERPER_API_KEY",
    "SENTRY_DSN", "DASHBOARD_API_KEY",
    "ENABLE_LIVE_TRADING",
    "DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
    "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO",
]


def _parse_env_file() -> dict[str, str]:
    """Parse the .env file into a dict."""
    result: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return result
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        result[key] = value
    return result


def _mask_value(value: str) -> str:
    """Mask a secret value for display: show first 4 and last 4 chars."""
    if not value:
        return ""
    if len(value) <= 12:
        return "•" * len(value)
    return value[:4] + "•" * (len(value) - 8) + value[-4:]


@app.route("/api/env")
def api_env() -> Any:
    """Return all managed env vars with masked values."""
    env_data = _parse_env_file()
    items = []
    for key in _MANAGED_ENV_KEYS:
        raw = env_data.get(key, "") or os.environ.get(key, "")
        items.append({
            "key": key,
            "is_set": bool(raw),
            "masked_value": _mask_value(raw) if raw else "",
            "is_secret": key not in ("ENABLE_LIVE_TRADING", "POLYMARKET_CHAIN_ID"),
        })
    return jsonify({"items": items})


@app.route("/api/env", methods=["POST"])
def api_env_save() -> Any:
    """Update one or more env vars. Writes to .env and reloads into os.environ.

    Body: { "vars": { "KEY": "value", ... } }
    """
    data = request.get_json(force=True)
    if not data or "vars" not in data:
        return jsonify({"ok": False, "error": "Body must contain 'vars' dict"}), 400

    new_vars: dict[str, str] = data["vars"]

    # Security: only allow managed keys
    for key in new_vars:
        if key not in _MANAGED_ENV_KEYS:
            return jsonify({"ok": False, "error": f"Key '{key}' is not a managed env var"}), 400

    try:
        # Read existing .env preserving comments and structure
        lines: list[str] = []
        if _ENV_FILE.exists():
            lines = _ENV_FILE.read_text().splitlines()

        # Build a map of existing key -> line index
        key_line_map: dict[str, int] = {}
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                key_line_map[k] = i

        # Update existing or append new
        for key, value in new_vars.items():
            # Skip empty values (don't write blanks)
            if value == "":
                continue
            new_line = f'{key}={value}'
            if key in key_line_map:
                lines[key_line_map[key]] = new_line
            else:
                lines.append(new_line)

            # Also update os.environ immediately
            os.environ[key] = value

        # Write back
        _ENV_FILE.write_text("\n".join(lines) + "\n")

        return jsonify({
            "ok": True,
            "message": f"Updated {len(new_vars)} environment variable(s)",
            "keys_updated": list(new_vars.keys()),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ─── API: Feature Flags Toggle ────────────────────────────────────

@app.route("/api/flags", methods=["POST"])
def api_flags_toggle() -> Any:
    """Toggle a feature flag. Writes to config.yaml and hot-reloads.

    Body: { "flag": "ensemble_enabled", "value": true }
    """
    global _config
    data = request.get_json(force=True)
    flag = data.get("flag", "")
    value = data.get("value")

    # Map flag names to config paths
    FLAG_MAP = {
        "ensemble_enabled": ("ensemble", "enabled"),
        "drawdown_enabled": ("drawdown", "enabled"),
        "wallet_scanner_enabled": ("wallet_scanner", "enabled"),
        "alerts_enabled": ("alerts", "enabled"),
        "cache_enabled": ("cache", "enabled"),
        "twap_enabled": ("execution", "twap_enabled"),
        "adaptive_pricing": ("execution", "adaptive_pricing"),
        "dry_run": ("execution", "dry_run"),
        "kill_switch": ("risk", "kill_switch"),
        "paper_mode": ("engine", "paper_mode"),
        "auto_start": ("engine", "auto_start"),
        "daily_summary": ("alerts", "daily_summary_enabled"),
        "metrics_enabled": ("observability", "enable_metrics"),
        "fetch_full_content": ("research", "fetch_full_content"),
        "track_leaderboard": ("wallet_scanner", "track_leaderboard"),
    }

    if flag not in FLAG_MAP:
        return jsonify({"ok": False, "error": f"Unknown flag: {flag}"}), 400

    section_key, field_key = FLAG_MAP[flag]

    try:
        # Load current YAML
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH) as f:
                raw: dict[str, Any] = yaml.safe_load(f) or {}
        else:
            raw = {}

        # Set the value
        if section_key not in raw:
            raw[section_key] = {}
        raw[section_key][field_key] = bool(value)

        # Validate
        new_config = BotConfig(**raw)

        # Write
        with open(_CONFIG_PATH, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        _config = new_config

        # Handle ENABLE_LIVE_TRADING specially (env var)
        if flag == "live_trading":
            os.environ["ENABLE_LIVE_TRADING"] = "true" if value else "false"

        return jsonify({
            "ok": True,
            "message": f"Flag '{flag}' set to {value}",
            "flag": flag,
            "value": bool(value),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ─── API: Config Schema (for dynamic form generation) ─────────────

@app.route("/api/config/schema")
def api_config_schema() -> Any:
    """Return config schema: section → field → {type, default, description}.

    Used by the Settings UI to auto-generate form fields.
    """
    from src.config import (
        ScanningConfig, ResearchConfig, ForecastingConfig, EnsembleConfig,
        RiskConfig, DrawdownConfig, PortfolioConfig, TimelineConfig,
        MicrostructureConfig, ExecutionConfig, StorageConfig, CacheConfig,
        ObservabilityConfig, AlertsConfig, EngineConfig, WalletScannerConfig,
    )

    sections = {
        "scanning": ScanningConfig,
        "research": ResearchConfig,
        "forecasting": ForecastingConfig,
        "ensemble": EnsembleConfig,
        "risk": RiskConfig,
        "drawdown": DrawdownConfig,
        "portfolio": PortfolioConfig,
        "timeline": TimelineConfig,
        "microstructure": MicrostructureConfig,
        "execution": ExecutionConfig,
        "storage": StorageConfig,
        "cache": CacheConfig,
        "observability": ObservabilityConfig,
        "alerts": AlertsConfig,
        "engine": EngineConfig,
        "wallet_scanner": WalletScannerConfig,
    }

    schema: dict[str, Any] = {}
    for section_name, model_cls in sections.items():
        fields_info: dict[str, Any] = {}
        try:
            for field_name, field_info in model_cls.model_fields.items():
                annotation = field_info.annotation
                type_str = "string"
                try:
                    if annotation is bool:
                        type_str = "bool"
                    elif annotation is int:
                        type_str = "int"
                    elif annotation is float:
                        type_str = "float"
                    elif annotation is str:
                        type_str = "string"
                    elif hasattr(annotation, "__origin__"):
                        origin = getattr(annotation, "__origin__", None)
                        if origin is list:
                            type_str = "list"
                        elif origin is dict:
                            type_str = "dict"
                except Exception:
                    type_str = "string"

                # Safely extract default
                default_val = None
                try:
                    d = field_info.default
                    if d is not None and not callable(d):
                        # Verify it's JSON-serializable
                        import json
                        json.dumps(d)
                        default_val = d
                except Exception:
                    default_val = None

                desc = ""
                try:
                    desc = field_info.description or ""
                except Exception:
                    pass

                fields_info[field_name] = {
                    "type": type_str,
                    "default": default_val,
                    "description": desc,
                }
        except Exception:
            pass
        schema[section_name] = fields_info

    return jsonify({"schema": schema})


# ─── API: Export Data ──────────────────────────────────────────────

@app.route("/api/export/<table_name>")
def api_export(table_name: str) -> Any:
    """Export table data as JSON (for CSV conversion on client)."""
    allowed = {"forecasts", "trades", "positions", "markets"}
    if table_name not in allowed:
        return jsonify({"error": f"Unknown table: {table_name}"}), 404
    conn = _get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute(f"SELECT * FROM {table_name} ORDER BY rowid DESC").fetchall()
        data = [dict(r) for r in rows]
        return jsonify({"table": table_name, "count": len(data), "rows": data})
    finally:
        conn.close()


# ─── Runner ────────────────────────────────────────────────────────

def run_dashboard(
    config_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = 2345,
    debug: bool = False,
    start_engine: bool = True,
) -> None:
    """Start the dashboard Flask server and optionally the trading engine."""
    global _config, _db_path

    _config = load_config(config_path)
    _db_path = _config.storage.sqlite_path

    # Ensure DB directory exists
    Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

    # Ensure tables exist
    conn = _get_conn()
    _ensure_tables(conn)
    conn.close()

    print(f"\n  🚀 Polymarket Bot Dashboard")
    print(f"  ➜  http://{host}:{port}")

    # Auto-start the trading engine in a background thread
    if start_engine:
        print(f"  🤖 Trading engine auto-starting in background…")
        print(f"     Cycle interval: {_config.engine.cycle_interval_secs}s")
        print(f"     Paper mode: {_config.engine.paper_mode}")
        print(f"     Live trading: {is_live_trading_enabled()}")
        _start_engine(_config)
    else:
        print(f"  ⚠️  Engine not auto-started (use dashboard button to start)")

    # Start maintenance worker (equity snapshots, scheduled backup, VACUUM)
    _start_maintenance()
    print(f"  🔧 Maintenance worker started (snapshots / backup / vacuum)")

    print()
    app.run(host=host, port=port, debug=debug)

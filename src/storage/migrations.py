"""Database migrations — create and upgrade schema."""

from __future__ import annotations

import sqlite3

from src.observability.logger import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 10

_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS markets (
            id TEXT PRIMARY KEY,
            condition_id TEXT,
            question TEXT,
            market_type TEXT,
            category TEXT,
            volume REAL DEFAULT 0,
            liquidity REAL DEFAULT 0,
            end_date TEXT,
            resolution_source TEXT,
            first_seen TEXT,
            last_updated TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS forecasts (
            id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            question TEXT,
            market_type TEXT,
            implied_probability REAL,
            model_probability REAL,
            edge REAL,
            confidence_level TEXT,
            evidence_quality REAL,
            num_sources INTEGER,
            decision TEXT,
            reasoning TEXT,
            evidence_json TEXT,
            invalidation_triggers_json TEXT,
            created_at TEXT,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            order_id TEXT UNIQUE,
            market_id TEXT NOT NULL,
            token_id TEXT,
            side TEXT,
            price REAL,
            size REAL,
            stake_usd REAL,
            status TEXT,
            dry_run INTEGER DEFAULT 1,
            created_at TEXT,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS positions (
            market_id TEXT PRIMARY KEY,
            token_id TEXT,
            direction TEXT,
            entry_price REAL,
            size REAL,
            stake_usd REAL,
            current_price REAL,
            pnl REAL,
            opened_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_forecasts_market ON forecasts(market_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_forecasts_created ON forecasts(created_at);
        """,
    ],
    2: [
        # Audit trail table
        """
        CREATE TABLE IF NOT EXISTS audit_trail (
            id TEXT PRIMARY KEY,
            timestamp REAL NOT NULL,
            market_id TEXT,
            decision TEXT,
            stage TEXT,
            data_json TEXT,
            checksum TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_trail(timestamp);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audit_market ON audit_trail(market_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audit_decision ON audit_trail(decision);
        """,

        # Calibration history
        """
        CREATE TABLE IF NOT EXISTS calibration_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_prob REAL NOT NULL,
            actual_outcome REAL NOT NULL,
            recorded_at REAL NOT NULL,
            market_id TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_calibration_recorded ON calibration_history(recorded_at);
        """,

        # Fill tracking
        """
        CREATE TABLE IF NOT EXISTS fill_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            market_id TEXT,
            expected_price REAL,
            fill_price REAL,
            size_ordered REAL,
            size_filled REAL,
            slippage_bps REAL,
            time_to_fill_secs REAL,
            execution_strategy TEXT,
            timestamp REAL
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_fills_order ON fill_records(order_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_fills_market ON fill_records(market_id);
        """,

        # Enhanced positions table with more tracking fields
        """
        CREATE TABLE IF NOT EXISTS positions_v2 (
            market_id TEXT PRIMARY KEY,
            question TEXT,
            category TEXT,
            event_slug TEXT,
            side TEXT,
            size_usd REAL,
            entry_price REAL,
            entry_time REAL,
            current_price REAL DEFAULT 0,
            unrealised_pnl REAL DEFAULT 0,
            realised_pnl REAL DEFAULT 0,
            status TEXT DEFAULT 'open',
            exit_time REAL DEFAULT 0,
            exit_price REAL DEFAULT 0,
            exit_reason TEXT DEFAULT '',
            entry_model_prob REAL DEFAULT 0,
            entry_edge REAL DEFAULT 0,
            entry_confidence TEXT DEFAULT 'LOW',
            stop_loss_price REAL DEFAULT 0,
            take_profit_price REAL DEFAULT 0,
            max_unrealised_pnl REAL DEFAULT 0,
            min_unrealised_pnl REAL DEFAULT 0
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_posv2_status ON positions_v2(status);
        """,

        # Drawdown state
        """
        CREATE TABLE IF NOT EXISTS drawdown_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            equity REAL,
            peak_equity REAL,
            drawdown_pct REAL,
            heat_level INTEGER
        );
        """,

        # Event triggers
        """
        CREATE TABLE IF NOT EXISTS event_triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            trigger_type TEXT,
            severity TEXT,
            details TEXT,
            timestamp REAL
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_events_market ON event_triggers(market_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_events_timestamp ON event_triggers(timestamp);
        """,
    ],
    3: [
        # Engine state — persisted between engine and dashboard processes
        """
        CREATE TABLE IF NOT EXISTS engine_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at REAL
        );
        """,
        # Candidate log — every market evaluated per cycle
        """
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL,
            market_id TEXT NOT NULL,
            question TEXT,
            market_type TEXT,
            implied_prob REAL,
            model_prob REAL,
            edge REAL,
            evidence_quality REAL,
            num_sources INTEGER DEFAULT 0,
            confidence TEXT,
            decision TEXT,
            decision_reasons TEXT,
            stake_usd REAL DEFAULT 0,
            order_status TEXT DEFAULT '',
            created_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_candidates_cycle ON candidates(cycle_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_candidates_created ON candidates(created_at);
        """,
        # Alerts log — persisted alerts for dashboard
        """
        CREATE TABLE IF NOT EXISTS alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL,
            channel TEXT DEFAULT 'system',
            message TEXT NOT NULL,
            market_id TEXT DEFAULT '',
            created_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts_log(created_at);
        """,
    ],
    4: [
        # Rich research evidence with real source URLs, titles, quality breakdown
        """
        ALTER TABLE forecasts ADD COLUMN research_evidence_json TEXT DEFAULT '{}';
        """,
    ],
    5: [
        # ── Performance Analytics Tables ──

        # Trade performance log (populated when markets resolve)
        """
        CREATE TABLE IF NOT EXISTS performance_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            question TEXT,
            category TEXT DEFAULT 'UNKNOWN',
            forecast_prob REAL,
            actual_outcome REAL,
            edge_at_entry REAL,
            confidence TEXT DEFAULT 'LOW',
            evidence_quality REAL DEFAULT 0,
            stake_usd REAL DEFAULT 0,
            entry_price REAL DEFAULT 0,
            exit_price REAL DEFAULT 0,
            pnl REAL DEFAULT 0,
            holding_hours REAL DEFAULT 0,
            resolved_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_perf_resolved ON performance_log(resolved_at);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_perf_category ON performance_log(category);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_perf_market ON performance_log(market_id);
        """,

        # Per-model forecast accuracy log (for adaptive weighting)
        """
        CREATE TABLE IF NOT EXISTS model_forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            market_id TEXT NOT NULL,
            category TEXT DEFAULT 'UNKNOWN',
            forecast_prob REAL,
            actual_outcome REAL,
            recorded_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_model_log_model ON model_forecast_log(model_name);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_model_log_category ON model_forecast_log(category);
        """,

        # Market regime history
        """
        CREATE TABLE IF NOT EXISTS regime_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regime TEXT NOT NULL,
            confidence REAL DEFAULT 0,
            kelly_multiplier REAL DEFAULT 1.0,
            size_multiplier REAL DEFAULT 1.0,
            explanation TEXT,
            detected_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_regime_detected ON regime_history(detected_at);
        """,

        # Smart entry plans log
        """
        CREATE TABLE IF NOT EXISTS smart_entry_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            side TEXT,
            current_price REAL,
            recommended_price REAL,
            strategy TEXT,
            improvement_bps REAL DEFAULT 0,
            vwap_signal TEXT,
            depth_signal TEXT,
            momentum_signal TEXT,
            created_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_smart_entry_market ON smart_entry_log(market_id);
        """,

        # Scanner pipeline state (for live scanner view)
        """
        CREATE TABLE IF NOT EXISTS scanner_pipeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER,
            market_id TEXT NOT NULL,
            question TEXT,
            stage TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            details_json TEXT DEFAULT '{}',
            updated_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_scanner_cycle ON scanner_pipeline(cycle_id);
        """,
    ],

    # ── Migration 6: Whale / Wallet Scanner tables ───────────────
    6: [
        # Tracked whale wallets
        """
        CREATE TABLE IF NOT EXISTS tracked_wallets (
            address TEXT PRIMARY KEY,
            name TEXT,
            total_pnl REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            active_positions INTEGER DEFAULT 0,
            total_volume REAL DEFAULT 0,
            score REAL DEFAULT 0,
            last_scanned TEXT
        );
        """,

        # Conviction signals (multi-whale consensus on a market)
        """
        CREATE TABLE IF NOT EXISTS wallet_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_slug TEXT NOT NULL,
            title TEXT,
            condition_id TEXT,
            outcome TEXT,
            whale_count INTEGER DEFAULT 0,
            total_whale_usd REAL DEFAULT 0,
            avg_whale_price REAL DEFAULT 0,
            current_price REAL DEFAULT 0,
            conviction_score REAL DEFAULT 0,
            whale_names_json TEXT DEFAULT '[]',
            direction TEXT,
            signal_strength TEXT,
            detected_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_wallet_signals_market
            ON wallet_signals(market_slug);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_wallet_signals_detected
            ON wallet_signals(detected_at);
        """,

        # Position change deltas (new entries / exits)
        """
        CREATE TABLE IF NOT EXISTS wallet_deltas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_address TEXT NOT NULL,
            wallet_name TEXT,
            action TEXT NOT NULL,
            market_slug TEXT,
            title TEXT,
            outcome TEXT,
            size_change REAL DEFAULT 0,
            value_change_usd REAL DEFAULT 0,
            current_price REAL DEFAULT 0,
            detected_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_wallet_deltas_detected
            ON wallet_deltas(detected_at);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_wallet_deltas_wallet
            ON wallet_deltas(wallet_address);
        """,
    ],

    # ── Migration 7: Deduplication constraints for wallet tables ──
    7: [
        # Remove duplicate wallet_signals keeping only the latest row per (market_slug, outcome)
        """
        DELETE FROM wallet_signals
        WHERE id NOT IN (
            SELECT MAX(id) FROM wallet_signals
            GROUP BY market_slug, outcome
        );
        """,
        # Add unique constraint so each market+outcome has at most one signal row
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_signals_unique
            ON wallet_signals(market_slug, outcome);
        """,
        # Remove duplicate wallet_deltas keeping only the latest per (wallet_address, market_slug, outcome, action)
        """
        DELETE FROM wallet_deltas
        WHERE id NOT IN (
            SELECT MAX(id) FROM wallet_deltas
            GROUP BY wallet_address, market_slug, outcome, action
        );
        """,
        # Add unique constraint so each wallet+market+outcome+action has at most one delta row
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_deltas_unique
            ON wallet_deltas(wallet_address, market_slug, outcome, action);
        """,
    ],

    # ── Migration 8: Paper trading improvements ──────────────────
    8: [
        # Add question/market_type to positions for better context
        """
        ALTER TABLE positions ADD COLUMN question TEXT DEFAULT '';
        """,
        """
        ALTER TABLE positions ADD COLUMN market_type TEXT DEFAULT '';
        """,

        # Closed positions archive — permanent record of every closed position
        """
        CREATE TABLE IF NOT EXISTS closed_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            token_id TEXT,
            direction TEXT,
            entry_price REAL DEFAULT 0,
            exit_price REAL DEFAULT 0,
            size REAL DEFAULT 0,
            stake_usd REAL DEFAULT 0,
            pnl REAL DEFAULT 0,
            close_reason TEXT DEFAULT '',
            question TEXT DEFAULT '',
            market_type TEXT DEFAULT '',
            opened_at TEXT,
            closed_at TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_closed_pos_market ON closed_positions(market_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_closed_pos_closed ON closed_positions(closed_at);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_closed_pos_reason ON closed_positions(close_reason);
        """,
    ],

    # ── Migration 9: Watchlist, Trade Journal, Equity Snapshots, VaR ──
    9: [
        # Market watchlist — user-pinned markets to always track
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            market_id TEXT PRIMARY KEY,
            question TEXT,
            category TEXT DEFAULT '',
            added_at TEXT DEFAULT (datetime('now')),
            notes TEXT DEFAULT ''
        );
        """,

        # Trade journal — AI-annotated trade records for learning
        """
        CREATE TABLE IF NOT EXISTS trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            question TEXT,
            direction TEXT,
            entry_price REAL DEFAULT 0,
            exit_price REAL DEFAULT 0,
            stake_usd REAL DEFAULT 0,
            pnl REAL DEFAULT 0,
            annotation TEXT DEFAULT '',
            reasoning TEXT DEFAULT '',
            lessons_learned TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_journal_market ON trade_journal(market_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_journal_created ON trade_journal(created_at);
        """,

        # Equity snapshots — periodic snapshots for P&L curve
        """
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            equity REAL NOT NULL,
            invested REAL DEFAULT 0,
            cash REAL DEFAULT 0,
            unrealised_pnl REAL DEFAULT 0,
            realised_pnl REAL DEFAULT 0,
            num_positions INTEGER DEFAULT 0,
            daily_var REAL DEFAULT 0,
            drawdown_pct REAL DEFAULT 0
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_equity_snap_ts ON equity_snapshots(timestamp);
        """,

        # VaR history — daily Value at Risk calculations
        """
        CREATE TABLE IF NOT EXISTS var_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            daily_var_95 REAL DEFAULT 0,
            daily_var_99 REAL DEFAULT 0,
            portfolio_value REAL DEFAULT 0,
            num_positions INTEGER DEFAULT 0,
            method TEXT DEFAULT 'parametric',
            details_json TEXT DEFAULT '{}'
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_var_ts ON var_history(timestamp);
        """,
    ],

    # ── Migration 10: Strategies & Wallets ───────────────────────
    10: [
        # User-managed wallets (live Polymarket wallets + paper/simulated wallets)
        """
        CREATE TABLE IF NOT EXISTS wallets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            wallet_type TEXT NOT NULL DEFAULT 'paper',
            address TEXT DEFAULT '',
            initial_balance REAL DEFAULT 10000,
            current_balance REAL DEFAULT 10000,
            total_pnl REAL DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            win_count INTEGER DEFAULT 0,
            loss_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            color TEXT DEFAULT '#4c8dff',
            icon TEXT DEFAULT '💰',
            notes TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        );
        """,

        # Trading strategies that users can create and assign to wallets
        """
        CREATE TABLE IF NOT EXISTS strategies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            strategy_type TEXT NOT NULL DEFAULT 'ai_trading',
            description TEXT DEFAULT '',
            config_json TEXT DEFAULT '{}',
            risk_profile TEXT DEFAULT 'moderate',
            is_active INTEGER DEFAULT 1,
            icon TEXT DEFAULT '🤖',
            color TEXT DEFAULT '#00e68a',
            created_at TEXT,
            updated_at TEXT
        );
        """,

        # Many-to-many: a strategy runs on a wallet (one wallet can have many strategies)
        """
        CREATE TABLE IF NOT EXISTS strategy_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            wallet_id TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            allocated_balance REAL DEFAULT 0,
            current_pnl REAL DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            win_count INTEGER DEFAULT 0,
            loss_count INTEGER DEFAULT 0,
            last_trade_at TEXT,
            created_at TEXT,
            FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY (wallet_id) REFERENCES wallets(id) ON DELETE CASCADE,
            UNIQUE(strategy_id, wallet_id)
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_sw_strategy ON strategy_wallets(strategy_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_sw_wallet ON strategy_wallets(wallet_id);
        """,

        # Per-wallet trade log (links trades to wallets and strategies)
        """
        CREATE TABLE IF NOT EXISTS wallet_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_id TEXT NOT NULL,
            strategy_id TEXT DEFAULT '',
            market_id TEXT NOT NULL,
            question TEXT DEFAULT '',
            side TEXT DEFAULT '',
            entry_price REAL DEFAULT 0,
            exit_price REAL DEFAULT 0,
            size REAL DEFAULT 0,
            stake_usd REAL DEFAULT 0,
            pnl REAL DEFAULT 0,
            status TEXT DEFAULT 'open',
            is_paper INTEGER DEFAULT 1,
            opened_at TEXT,
            closed_at TEXT,
            FOREIGN KEY (wallet_id) REFERENCES wallets(id) ON DELETE CASCADE
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_wt_wallet ON wallet_trades(wallet_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_wt_strategy ON wallet_trades(strategy_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_wt_status ON wallet_trades(status);
        """,

        # Per-wallet equity snapshots for individual wallet P&L curves
        """
        CREATE TABLE IF NOT EXISTS wallet_equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            equity REAL DEFAULT 0,
            invested REAL DEFAULT 0,
            cash REAL DEFAULT 0,
            unrealised_pnl REAL DEFAULT 0,
            realised_pnl REAL DEFAULT 0,
            num_positions INTEGER DEFAULT 0,
            FOREIGN KEY (wallet_id) REFERENCES wallets(id) ON DELETE CASCADE
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_we_wallet ON wallet_equity_snapshots(wallet_id);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_we_ts ON wallet_equity_snapshots(timestamp);
        """,

        # Seed a default paper wallet so the dashboard is not empty
        """
        INSERT OR IGNORE INTO wallets (id, name, wallet_type, initial_balance, current_balance, color, icon, created_at, updated_at)
        VALUES ('default-paper', 'Paper Trading Wallet', 'paper', 10000, 10000, '#4c8dff', '📄', datetime('now'), datetime('now'));
        """,
        # Seed the default AI Trading strategy
        """
        INSERT OR IGNORE INTO strategies (id, name, strategy_type, description, risk_profile, icon, color, created_at, updated_at)
        VALUES ('default-ai', 'AI Trading', 'ai_trading', 'Automated AI-powered trading using multi-model ensemble forecasting', 'moderate', '🤖', '#00e68a', datetime('now'), datetime('now'));
        """,
        # Bind default strategy to default wallet
        """
        INSERT OR IGNORE INTO strategy_wallets (strategy_id, wallet_id, allocated_balance, created_at)
        VALUES ('default-ai', 'default-paper', 10000, datetime('now'));
        """,
    ],
}


def run_migrations(conn: sqlite3.Connection) -> None:
    """Run all pending migrations."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    conn.commit()

    current = _get_current_version(conn)

    for version in sorted(_MIGRATIONS.keys()):
        if version <= current:
            continue
        log.info("migrations.running", version=version)
        for sql in _MIGRATIONS[version]:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as e:
                # Handle idempotent ALTER TABLE ADD COLUMN when column already exists
                # (e.g. dashboard _ensure_tables created it first)
                if "duplicate column name" in str(e):
                    log.info("migrations.column_exists_skip", version=version, error=str(e))
                else:
                    raise
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (version,),
        )
        conn.commit()
        log.info("migrations.applied", version=version)

    final = _get_current_version(conn)
    log.info("migrations.complete", version=final)


def _get_current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] if row and row[0] else 0
    except Exception:
        return 0

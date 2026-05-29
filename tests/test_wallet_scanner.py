"""Tests for wallet scanner — Data API client, WalletScanner, conviction
signals, delta detection, scoring, database persistence, and config.

Covers:
  - DataAPIClient: parsing positions & activity, error handling
  - WalletScanner: scanning, scoring, conviction, deltas
  - Database: save_scan_result, migration v6
  - Config: WalletScannerConfig defaults
  - Integration: end-to-end scan cycle
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════
#  HELPER: Create test database with wallet scanner tables
# ═══════════════════════════════════════════════════════════════════

def _create_test_db() -> sqlite3.Connection:
    """Create in-memory SQLite DB with wallet scanner schema (migration 6)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
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
    """)
    return conn


# ═══════════════════════════════════════════════════════════════════
#  DATA API CLIENT: Parsing
# ═══════════════════════════════════════════════════════════════════

class TestParsePosition:
    """Test _parse_position with various API response shapes."""

    def test_parse_full_position(self):
        from src.connectors.polymarket_data import _parse_position
        raw = {
            "proxyWallet": "0xabc123",
            "asset": "tok_001",
            "conditionId": "cond_xyz",
            "slug": "will-btc-hit-100k",
            "title": "Will BTC hit $100k?",
            "outcome": "Yes",
            "size": 500.0,
            "avgPrice": 0.65,
            "curPrice": 0.78,
            "initialValue": 325.0,
            "currentValue": 390.0,
            "cashPnl": 65.0,
            "percentPnl": 20.0,
            "endDate": "2026-06-01",
            "realized": False,
        }
        pos = _parse_position(raw)
        assert pos.proxy_wallet == "0xabc123"
        assert pos.asset == "tok_001"
        assert pos.condition_id == "cond_xyz"
        assert pos.market_slug == "will-btc-hit-100k"
        assert pos.title == "Will BTC hit $100k?"
        assert pos.outcome == "Yes"
        assert pos.size == 500.0
        assert pos.avg_price == 0.65
        assert pos.cur_price == 0.78
        assert pos.initial_value == 325.0
        assert pos.current_value == 390.0
        assert pos.cash_pnl == 65.0
        assert pos.percent_pnl == 20.0
        assert pos.end_date == "2026-06-01"
        assert pos.realized is False

    def test_parse_empty_position(self):
        from src.connectors.polymarket_data import _parse_position
        pos = _parse_position({})
        assert pos.proxy_wallet == ""
        assert pos.size == 0.0
        assert pos.cash_pnl == 0.0

    def test_parse_snake_case_keys(self):
        from src.connectors.polymarket_data import _parse_position
        raw = {
            "proxy_wallet": "0xdef456",
            "condition_id": "cond_abc",
            "avg_price": 0.50,
            "cur_price": 0.70,
            "initial_value": 100.0,
            "current_value": 140.0,
            "cash_pnl": 40.0,
            "percent_pnl": 40.0,
        }
        pos = _parse_position(raw)
        assert pos.proxy_wallet == "0xdef456"
        assert pos.condition_id == "cond_abc"
        assert pos.avg_price == 0.50
        assert pos.cash_pnl == 40.0

    def test_position_is_profitable(self):
        from src.connectors.polymarket_data import WalletPosition
        assert WalletPosition(cash_pnl=10.0).is_profitable is True
        assert WalletPosition(cash_pnl=-5.0).is_profitable is False
        assert WalletPosition(cash_pnl=0.0).is_profitable is False

    def test_position_unrealised_return(self):
        from src.connectors.polymarket_data import WalletPosition
        pos = WalletPosition(initial_value=100.0, current_value=150.0)
        assert pos.unrealised_return_pct == 50.0

    def test_position_unrealised_return_zero_initial(self):
        from src.connectors.polymarket_data import WalletPosition
        pos = WalletPosition(initial_value=0.0, current_value=50.0)
        assert pos.unrealised_return_pct == 0.0

    def test_position_to_dict(self):
        from src.connectors.polymarket_data import WalletPosition
        pos = WalletPosition(
            proxy_wallet="0x123", asset="tok", title="Test",
            size=10.5, cash_pnl=5.25, realized=True,
        )
        d = pos.to_dict()
        assert d["proxy_wallet"] == "0x123"
        assert d["size"] == 10.5
        assert d["cash_pnl"] == 5.25
        assert d["realized"] is True


class TestParseActivity:
    """Test _parse_activity with various formats."""

    def test_parse_full_activity(self):
        from src.connectors.polymarket_data import _parse_activity
        raw = {
            "transactionHash": "0xtxhash",
            "type": "Buy",
            "slug": "will-eth-merge",
            "title": "Will ETH merge?",
            "outcome": "Yes",
            "size": 100.0,
            "price": 0.55,
            "value": 55.0,
            "timestamp": "2026-01-15T10:00:00Z",
        }
        act = _parse_activity(raw)
        assert act.transaction_hash == "0xtxhash"
        assert act.action == "Buy"
        assert act.market_slug == "will-eth-merge"
        assert act.size == 100.0
        assert act.price == 0.55
        assert act.value_usd == 55.0

    def test_parse_activity_computed_value(self):
        from src.connectors.polymarket_data import _parse_activity
        raw = {"size": 200.0, "price": 0.30}
        act = _parse_activity(raw)
        assert act.value_usd == 60.0  # 200 * 0.30

    def test_parse_empty_activity(self):
        from src.connectors.polymarket_data import _parse_activity
        act = _parse_activity({})
        assert act.action == ""
        assert act.size == 0.0
        assert act.value_usd == 0.0

    def test_activity_to_dict(self):
        from src.connectors.polymarket_data import WalletActivity
        act = WalletActivity(
            action="Sell", market_slug="test-market",
            size=50.0, price=0.80, value_usd=40.0,
        )
        d = act.to_dict()
        assert d["action"] == "Sell"
        assert d["size"] == 50.0
        assert d["value_usd"] == 40.0


# ═══════════════════════════════════════════════════════════════════
#  DATA API CLIENT: Async methods
# ═══════════════════════════════════════════════════════════════════

class TestDataAPIClient:
    """Test DataAPIClient async methods with mocked HTTP."""

    def test_get_positions_parses_list(self):
        from src.connectors.polymarket_data import DataAPIClient
        client = DataAPIClient()

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"slug": "market-a", "outcome": "Yes", "size": 100, "cashPnl": 50},
            {"slug": "market-b", "outcome": "No", "size": 200, "cashPnl": -30},
        ]
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        positions = asyncio.get_event_loop().run_until_complete(
            client.get_positions("0xtest")
        )
        assert len(positions) == 2
        assert positions[0].market_slug == "market-a"
        assert positions[1].cash_pnl == -30.0

    def test_get_positions_parses_dict_with_positions_key(self):
        from src.connectors.polymarket_data import DataAPIClient
        client = DataAPIClient()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "positions": [
                {"slug": "mk1", "size": 10, "outcome": "Yes"},
            ],
        }
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        positions = asyncio.get_event_loop().run_until_complete(
            client.get_positions("0xtest2")
        )
        assert len(positions) == 1
        assert positions[0].market_slug == "mk1"

    def test_get_activity_parses(self):
        from src.connectors.polymarket_data import DataAPIClient
        client = DataAPIClient()

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"type": "Buy", "slug": "test-mk", "size": 50, "price": 0.6},
        ]
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        activities = asyncio.get_event_loop().run_until_complete(
            client.get_activity("0xtest3")
        )
        assert len(activities) == 1
        assert activities[0].action == "Buy"
        assert activities[0].value_usd == 30.0


# ═══════════════════════════════════════════════════════════════════
#  WALLET SCANNER: Core logic
# ═══════════════════════════════════════════════════════════════════

def _make_position(slug="test-market", outcome="Yes", size=100, avg_price=0.5,
                   cur_price=0.7, initial_value=50, current_value=70,
                   cash_pnl=20, condition_id="cond1"):
    from src.connectors.polymarket_data import WalletPosition
    return WalletPosition(
        market_slug=slug, outcome=outcome, size=size,
        avg_price=avg_price, cur_price=cur_price,
        initial_value=initial_value, current_value=current_value,
        cash_pnl=cash_pnl, condition_id=condition_id,
        title=f"Will {slug} happen?",
    )


class TestWalletScoring:
    """Test wallet scoring logic."""

    def test_score_wallet_high_pnl(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[])
        positions = [
            _make_position(cash_pnl=100),
            _make_position(slug="mk2", cash_pnl=50),
            _make_position(slug="mk3", cash_pnl=-10),
        ]
        meta = scanner._score_wallet(
            "0xabc", "TestWhale", positions,
            {"pnl": 1_000_000},
        )
        assert meta.name == "TestWhale"
        assert meta.address == "0xabc"
        assert meta.active_positions == 3
        assert meta.win_rate == pytest.approx(2 / 3, abs=0.01)
        assert meta.score > 0
        assert meta.score <= 100

    def test_score_wallet_empty_positions(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[])
        meta = scanner._score_wallet("0x", "Empty", [], {"pnl": 0})
        assert meta.active_positions == 0
        assert meta.win_rate == 0.0
        assert meta.score >= 0

    def test_score_wallet_all_winners(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[])
        positions = [_make_position(slug=f"m{i}", cash_pnl=100) for i in range(5)]
        meta = scanner._score_wallet("0x", "Winner", positions, {"pnl": 500_000})
        assert meta.win_rate == 1.0
        assert meta.score > 30  # win_rate(30) + pnl(5) + activity(1) = 36

    def test_score_wallet_all_losers(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[])
        positions = [_make_position(slug=f"m{i}", cash_pnl=-50) for i in range(3)]
        meta = scanner._score_wallet("0x", "Loser", positions, {"pnl": 10_000})
        assert meta.win_rate == 0.0

    def test_tracked_wallet_to_dict(self):
        from src.analytics.wallet_scanner import TrackedWallet
        w = TrackedWallet(
            address="0xabc", name="Test", total_pnl=1000,
            win_rate=0.65, active_positions=5, score=75.5,
        )
        d = w.to_dict()
        assert d["address"] == "0xabc"
        assert d["name"] == "Test"
        assert d["score"] == 75.5


# ═══════════════════════════════════════════════════════════════════
#  WALLET SCANNER: Delta detection
# ═══════════════════════════════════════════════════════════════════

class TestDeltaDetection:
    """Test position change detection between scan cycles."""

    def test_first_scan_no_deltas(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[{"address": "0xa", "name": "A"}])
        positions = {"0xa": [_make_position()]}
        deltas = scanner._detect_deltas(positions, "2026-01-01T00:00:00Z")
        assert len(deltas) == 0  # first scan has no previous

    def test_new_entry_detected(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[{"address": "0xa", "name": "A"}])

        # Set up previous snapshot with one position
        scanner._prev_positions = {
            "0xa": {"market-1|Yes": _make_position(slug="market-1")}
        }

        # Current has a new position
        current = {"0xa": [
            _make_position(slug="market-1"),
            _make_position(slug="market-2"),
        ]}
        deltas = scanner._detect_deltas(current, "2026-01-01")
        new_entries = [d for d in deltas if d.action == "NEW_ENTRY"]
        assert len(new_entries) == 1
        assert new_entries[0].market_slug == "market-2"

    def test_exit_detected(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[{"address": "0xa", "name": "A"}])

        scanner._prev_positions = {
            "0xa": {
                "market-1|Yes": _make_position(slug="market-1"),
                "market-2|No": _make_position(slug="market-2", outcome="No"),
            }
        }

        # Current only has one
        current = {"0xa": [_make_position(slug="market-1")]}
        deltas = scanner._detect_deltas(current, "2026-01-01")
        exits = [d for d in deltas if d.action == "EXIT"]
        assert len(exits) == 1
        assert exits[0].market_slug == "market-2"

    def test_size_increase_detected(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[{"address": "0xa", "name": "A"}])

        scanner._prev_positions = {
            "0xa": {"market-1|Yes": _make_position(slug="market-1", size=100)}
        }

        current = {"0xa": [_make_position(slug="market-1", size=200)]}
        deltas = scanner._detect_deltas(current, "2026-01-01")
        increases = [d for d in deltas if d.action == "SIZE_INCREASE"]
        assert len(increases) == 1
        assert increases[0].size_change == pytest.approx(100, abs=1)

    def test_size_decrease_detected(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[{"address": "0xa", "name": "A"}])

        scanner._prev_positions = {
            "0xa": {"market-1|Yes": _make_position(slug="market-1", size=200)}
        }

        current = {"0xa": [_make_position(slug="market-1", size=50)]}
        deltas = scanner._detect_deltas(current, "2026-01-01")
        decreases = [d for d in deltas if d.action == "SIZE_DECREASE"]
        assert len(decreases) == 1
        assert decreases[0].size_change < 0

    def test_no_delta_for_small_change(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[{"address": "0xa", "name": "A"}])

        scanner._prev_positions = {
            "0xa": {"market-1|Yes": _make_position(slug="market-1", size=100)}
        }

        # Only 5% change — below 10% threshold
        current = {"0xa": [_make_position(slug="market-1", size=105)]}
        deltas = scanner._detect_deltas(current, "2026-01-01")
        assert len(deltas) == 0

    def test_delta_to_dict(self):
        from src.analytics.wallet_scanner import WalletDelta
        d = WalletDelta(
            wallet_address="0x1", wallet_name="W",
            action="NEW_ENTRY", market_slug="test",
            size_change=100, value_change_usd=50,
        )
        dd = d.to_dict()
        assert dd["action"] == "NEW_ENTRY"
        assert dd["size_change"] == 100


# ═══════════════════════════════════════════════════════════════════
#  WALLET SCANNER: Conviction signals
# ═══════════════════════════════════════════════════════════════════

class TestConvictionSignals:
    """Test multi-whale conviction signal computation."""

    def test_single_whale_no_signal(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[{"address": "0xa", "name": "A"}],
            min_whale_count=2,
        )
        positions = {
            "0xa": [_make_position(slug="market-1", current_value=1000)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert len(signals) == 0

    def test_two_whales_same_market_generates_signal(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "Alpha"},
                {"address": "0xb", "name": "Beta"},
            ],
            min_whale_count=2,
            min_conviction_score=0,  # accept all
        )
        positions = {
            "0xa": [_make_position(slug="shared-market", current_value=500)],
            "0xb": [_make_position(slug="shared-market", current_value=800)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert len(signals) == 1
        assert signals[0].market_slug == "shared-market"
        assert signals[0].whale_count == 2
        assert signals[0].total_whale_usd == 1300.0
        assert "Alpha" in signals[0].whale_names
        assert "Beta" in signals[0].whale_names

    def test_conviction_score_increases_with_whales(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": f"0x{i}", "name": f"W{i}"} for i in range(5)
            ],
            min_whale_count=2,
            min_conviction_score=0,
        )
        # All 5 whales in same market
        positions = {
            f"0x{i}": [_make_position(slug="hot-market", current_value=1000)]
            for i in range(5)
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert len(signals) == 1
        assert signals[0].whale_count == 5
        assert signals[0].conviction_score > 50  # 5 * 20 = 100

    def test_different_outcomes_separate_signals(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "A"},
                {"address": "0xb", "name": "B"},
                {"address": "0xc", "name": "C"},
                {"address": "0xd", "name": "D"},
            ],
            min_whale_count=2,
            min_conviction_score=0,
        )
        positions = {
            "0xa": [_make_position(slug="contested", outcome="Yes", current_value=500)],
            "0xb": [_make_position(slug="contested", outcome="Yes", current_value=600)],
            "0xc": [_make_position(slug="contested", outcome="No", current_value=400)],
            "0xd": [_make_position(slug="contested", outcome="No", current_value=300)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert len(signals) == 2
        slugs_outcomes = {(s.market_slug, s.outcome) for s in signals}
        assert ("contested", "Yes") in slugs_outcomes
        assert ("contested", "No") in slugs_outcomes

    def test_dust_positions_ignored(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "A"},
                {"address": "0xb", "name": "B"},
            ],
            min_whale_count=2,
            min_conviction_score=0,
        )
        positions = {
            "0xa": [_make_position(slug="dust-mk", current_value=0.5)],  # < $1
            "0xb": [_make_position(slug="dust-mk", current_value=0.3)],  # < $1
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert len(signals) == 0

    def test_signal_direction_bullish_for_yes(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "A"},
                {"address": "0xb", "name": "B"},
            ],
            min_whale_count=2,
            min_conviction_score=0,
        )
        positions = {
            "0xa": [_make_position(slug="bull-mk", outcome="Yes", current_value=500)],
            "0xb": [_make_position(slug="bull-mk", outcome="Yes", current_value=500)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert signals[0].direction == "BULLISH"

    def test_signal_direction_bearish_for_no(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "A"},
                {"address": "0xb", "name": "B"},
            ],
            min_whale_count=2,
            min_conviction_score=0,
        )
        positions = {
            "0xa": [_make_position(slug="bear-mk", outcome="No", current_value=500)],
            "0xb": [_make_position(slug="bear-mk", outcome="No", current_value=500)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert signals[0].direction == "BEARISH"

    def test_signal_strength_strong(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[{"address": f"0x{i}", "name": f"W{i}"} for i in range(4)],
            min_whale_count=2,
            min_conviction_score=0,
        )
        positions = {
            f"0x{i}": [_make_position(slug="strong-mk", current_value=10000)]
            for i in range(4)
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert signals[0].signal_strength == "STRONG"

    def test_signal_strength_moderate(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "A"},
                {"address": "0xb", "name": "B"},
            ],
            min_whale_count=2,
            min_conviction_score=0,
        )
        # 2 whales × 20 = 40, plus small usd factor → ~45-55
        positions = {
            "0xa": [_make_position(slug="mod-mk", current_value=100)],
            "0xb": [_make_position(slug="mod-mk", current_value=100)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert signals[0].signal_strength in ("MODERATE", "STRONG")

    def test_conviction_sorted_descending(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[{"address": f"0x{i}", "name": f"W{i}"} for i in range(4)],
            min_whale_count=2,
            min_conviction_score=0,
        )
        positions = {
            "0x0": [
                _make_position(slug="low-mk", current_value=50),
                _make_position(slug="high-mk", current_value=5000),
            ],
            "0x1": [
                _make_position(slug="low-mk", current_value=50),
                _make_position(slug="high-mk", current_value=5000),
            ],
            "0x2": [_make_position(slug="high-mk", current_value=5000)],
            "0x3": [_make_position(slug="high-mk", current_value=5000)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        if len(signals) >= 2:
            assert signals[0].conviction_score >= signals[1].conviction_score

    def test_conviction_signal_to_dict(self):
        from src.analytics.wallet_scanner import ConvictionSignal
        sig = ConvictionSignal(
            market_slug="test", whale_count=3,
            total_whale_usd=5000, conviction_score=75,
            whale_names=["A", "B", "C"],
            direction="BULLISH", signal_strength="STRONG",
        )
        d = sig.to_dict()
        assert d["whale_count"] == 3
        assert d["direction"] == "BULLISH"
        assert len(d["whale_names"]) == 3

    def test_min_conviction_filter(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "A"},
                {"address": "0xb", "name": "B"},
            ],
            min_whale_count=2,
            min_conviction_score=99.0,  # very high threshold
        )
        positions = {
            "0xa": [_make_position(slug="test-mk", current_value=100)],
            "0xb": [_make_position(slug="test-mk", current_value=100)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert len(signals) == 0  # filtered out by high threshold


# ═══════════════════════════════════════════════════════════════════
#  WALLET SCANNER: get_signal_for_market
# ═══════════════════════════════════════════════════════════════════

class TestGetSignalForMarket:
    """Test looking up signals by market slug."""

    def test_found(self):
        from src.analytics.wallet_scanner import WalletScanner, ConvictionSignal
        scanner = WalletScanner(wallets=[])
        signals = [
            ConvictionSignal(market_slug="mk1", conviction_score=50),
            ConvictionSignal(market_slug="mk2", conviction_score=70),
        ]
        result = scanner.get_signal_for_market("mk2", signals)
        assert result is not None
        assert result.conviction_score == 70

    def test_not_found(self):
        from src.analytics.wallet_scanner import WalletScanner, ConvictionSignal
        scanner = WalletScanner(wallets=[])
        signals = [ConvictionSignal(market_slug="mk1")]
        assert scanner.get_signal_for_market("mk999", signals) is None


# ═══════════════════════════════════════════════════════════════════
#  WALLET SCANNER: Full scan cycle
# ═══════════════════════════════════════════════════════════════════

class TestScanCycle:
    """Test end-to-end scan cycle with mocked API."""

    def test_scan_returns_result(self):
        from src.analytics.wallet_scanner import WalletScanner

        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            _make_position(slug="mk1", current_value=500, cash_pnl=100),
        ])

        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "Alpha", "pnl": 100_000},
                {"address": "0xb", "name": "Beta", "pnl": 80_000},
            ],
            client=mock_client,
            min_whale_count=2,
            min_conviction_score=0,
        )

        result = asyncio.get_event_loop().run_until_complete(scanner.scan())
        assert result.wallets_scanned == 2
        assert result.total_positions == 2
        assert len(result.tracked_wallets) == 2
        assert len(result.conviction_signals) >= 1

    def test_scan_handles_api_error(self):
        from src.analytics.wallet_scanner import WalletScanner

        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(side_effect=Exception("API down"))

        scanner = WalletScanner(
            wallets=[{"address": "0xa", "name": "A", "pnl": 0}],
            client=mock_client,
        )

        result = asyncio.get_event_loop().run_until_complete(scanner.scan())
        assert result.wallets_scanned == 0
        assert len(result.errors) == 1
        assert "API down" in result.errors[0]

    def test_second_scan_detects_deltas(self):
        from src.analytics.wallet_scanner import WalletScanner

        mock_client = AsyncMock()

        # First scan: one position
        mock_client.get_positions = AsyncMock(return_value=[
            _make_position(slug="mk1", current_value=500),
        ])

        scanner = WalletScanner(
            wallets=[{"address": "0xa", "name": "A", "pnl": 50_000}],
            client=mock_client,
            min_whale_count=1,  # single whale signals OK
            min_conviction_score=0,
        )

        asyncio.get_event_loop().run_until_complete(scanner.scan())

        # Second scan: added mk2
        mock_client.get_positions = AsyncMock(return_value=[
            _make_position(slug="mk1", current_value=500),
            _make_position(slug="mk2", current_value=300),
        ])

        result2 = asyncio.get_event_loop().run_until_complete(scanner.scan())
        entries = [d for d in result2.deltas if d.action == "NEW_ENTRY"]
        assert len(entries) == 1
        assert entries[0].market_slug == "mk2"

    def test_scan_result_to_dict(self):
        from src.analytics.wallet_scanner import ScanResult
        result = ScanResult(
            scanned_at="2026-01-01T00:00:00Z",
            wallets_scanned=5,
            total_positions=20,
        )
        d = result.to_dict()
        assert d["wallets_scanned"] == 5
        assert d["total_positions"] == 20
        assert isinstance(d["conviction_signals"], list)

    def test_snapshot_updated_after_scan(self):
        from src.analytics.wallet_scanner import WalletScanner

        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            _make_position(slug="mk1"),
        ])

        scanner = WalletScanner(
            wallets=[{"address": "0xa", "name": "A", "pnl": 0}],
            client=mock_client,
        )
        assert scanner._prev_positions == {}

        asyncio.get_event_loop().run_until_complete(scanner.scan())
        assert "0xa" in scanner._prev_positions
        assert "mk1|Yes" in scanner._prev_positions["0xa"]


# ═══════════════════════════════════════════════════════════════════
#  DATABASE: save_scan_result
# ═══════════════════════════════════════════════════════════════════

class TestSaveScanResult:
    """Test persisting scan results to SQLite."""

    def test_save_wallets(self):
        from src.analytics.wallet_scanner import (
            ScanResult, TrackedWallet, save_scan_result,
        )
        conn = _create_test_db()
        result = ScanResult(
            scanned_at="2026-01-01T00:00:00Z",
            tracked_wallets=[
                TrackedWallet(
                    address="0xabc", name="Test", total_pnl=1000,
                    win_rate=0.65, active_positions=5, score=70,
                    last_scanned="2026-01-01",
                ),
            ],
        )
        save_scan_result(conn, result)

        row = conn.execute("SELECT * FROM tracked_wallets WHERE address='0xabc'").fetchone()
        assert row is not None
        assert dict(row)["name"] == "Test"
        assert dict(row)["total_pnl"] == 1000
        conn.close()

    def test_save_signals(self):
        from src.analytics.wallet_scanner import (
            ScanResult, ConvictionSignal, save_scan_result,
        )
        conn = _create_test_db()
        result = ScanResult(
            scanned_at="2026-01-01",
            conviction_signals=[
                ConvictionSignal(
                    market_slug="test-mk", title="Test?",
                    whale_count=3, total_whale_usd=5000,
                    conviction_score=75, whale_names=["A", "B", "C"],
                    direction="BULLISH", signal_strength="STRONG",
                    detected_at="2026-01-01",
                ),
            ],
        )
        save_scan_result(conn, result)

        row = conn.execute("SELECT * FROM wallet_signals LIMIT 1").fetchone()
        assert row is not None
        d = dict(row)
        assert d["whale_count"] == 3
        assert d["direction"] == "BULLISH"
        names = json.loads(d["whale_names_json"])
        assert "A" in names
        conn.close()

    def test_save_deltas(self):
        from src.analytics.wallet_scanner import (
            ScanResult, WalletDelta, save_scan_result,
        )
        conn = _create_test_db()
        result = ScanResult(
            scanned_at="2026-01-01",
            deltas=[
                WalletDelta(
                    wallet_address="0x1", wallet_name="W1",
                    action="NEW_ENTRY", market_slug="mk1",
                    size_change=100, value_change_usd=50,
                    detected_at="2026-01-01",
                ),
            ],
        )
        save_scan_result(conn, result)

        row = conn.execute("SELECT * FROM wallet_deltas LIMIT 1").fetchone()
        assert row is not None
        d = dict(row)
        assert d["action"] == "NEW_ENTRY"
        assert d["wallet_name"] == "W1"
        conn.close()

    def test_save_empty_result(self):
        from src.analytics.wallet_scanner import ScanResult, save_scan_result
        conn = _create_test_db()
        result = ScanResult(scanned_at="2026-01-01")
        save_scan_result(conn, result)

        wallets = conn.execute("SELECT COUNT(*) FROM tracked_wallets").fetchone()[0]
        signals = conn.execute("SELECT COUNT(*) FROM wallet_signals").fetchone()[0]
        deltas = conn.execute("SELECT COUNT(*) FROM wallet_deltas").fetchone()[0]
        assert wallets == 0
        assert signals == 0
        assert deltas == 0
        conn.close()

    def test_save_wallet_upsert(self):
        from src.analytics.wallet_scanner import (
            ScanResult, TrackedWallet, save_scan_result,
        )
        conn = _create_test_db()

        # First save
        result1 = ScanResult(
            scanned_at="2026-01-01",
            tracked_wallets=[
                TrackedWallet(address="0xabc", name="V1", score=50),
            ],
        )
        save_scan_result(conn, result1)

        # Second save with updated score
        result2 = ScanResult(
            scanned_at="2026-01-02",
            tracked_wallets=[
                TrackedWallet(address="0xabc", name="V2", score=80),
            ],
        )
        save_scan_result(conn, result2)

        rows = conn.execute("SELECT * FROM tracked_wallets").fetchall()
        assert len(rows) == 1  # upserted, not duplicated
        assert dict(rows[0])["name"] == "V2"
        assert dict(rows[0])["score"] == 80
        conn.close()


# ═══════════════════════════════════════════════════════════════════
#  CONFIG: WalletScannerConfig
# ═══════════════════════════════════════════════════════════════════

class TestWalletScannerConfig:
    """Test WalletScannerConfig defaults and BotConfig integration."""

    def test_defaults(self):
        from src.config import WalletScannerConfig
        cfg = WalletScannerConfig()
        assert cfg.enabled is True
        assert cfg.scan_interval_minutes == 15
        assert cfg.min_whale_count == 1
        assert cfg.min_conviction_score == 15.0
        assert cfg.max_wallets == 20
        assert cfg.conviction_edge_boost == 0.08
        assert cfg.conviction_edge_penalty == 0.02
        assert cfg.whale_convergence_min_edge == 0.02
        assert cfg.track_leaderboard is True
        assert cfg.custom_wallets == []

    def test_bot_config_has_wallet_scanner(self):
        from src.config import BotConfig
        cfg = BotConfig()
        assert hasattr(cfg, "wallet_scanner")
        assert cfg.wallet_scanner.enabled is True
        assert cfg.wallet_scanner.scan_interval_minutes == 15

    def test_custom_values(self):
        from src.config import WalletScannerConfig
        cfg = WalletScannerConfig(
            enabled=False,
            scan_interval_minutes=60,
            min_whale_count=3,
            custom_wallets=["0x123", "0x456"],
        )
        assert cfg.enabled is False
        assert cfg.scan_interval_minutes == 60
        assert cfg.min_whale_count == 3
        assert len(cfg.custom_wallets) == 2


# ═══════════════════════════════════════════════════════════════════
#  MIGRATION: Schema version 6
# ═══════════════════════════════════════════════════════════════════

class TestMigration:
    """Test that migration v6 creates the wallet scanner tables."""

    def test_migration_creates_tables(self):
        from src.storage.migrations import run_migrations, SCHEMA_VERSION
        assert SCHEMA_VERSION >= 7

        conn = sqlite3.connect(":memory:")
        run_migrations(conn)

        # Check schema version
        v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert v >= 7

        # Check tables exist
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "tracked_wallets" in tables
        assert "wallet_signals" in tables
        assert "wallet_deltas" in tables
        conn.close()

    def test_migration_idempotent(self):
        from src.storage.migrations import run_migrations
        conn = sqlite3.connect(":memory:")
        run_migrations(conn)
        run_migrations(conn)  # run again — should be no-op
        v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert v >= 7
        conn.close()
    def test_signals_deduplication(self):
        """Saving the same conviction signal twice should not create duplicates."""
        from src.storage.migrations import run_migrations
        from src.analytics.wallet_scanner import save_scan_result, ScanResult, ConvictionSignal

        conn = sqlite3.connect(":memory:")
        run_migrations(conn)

        sig = ConvictionSignal(
            market_slug="test-market",
            title="Test Market",
            condition_id="0xabc",
            outcome="Yes",
            whale_count=3,
            total_whale_usd=50000.0,
            avg_whale_price=0.65,
            current_price=0.60,
            conviction_score=0.85,
            whale_names=["whale1", "whale2", "whale3"],
            direction="bullish",
            signal_strength="strong",
            detected_at="2026-01-01T00:00:00Z",
        )
        result = ScanResult(
            tracked_wallets=[],
            conviction_signals=[sig],
            deltas=[],
            scanned_at="2026-01-01T00:00:00Z",
        )

        # Save twice
        save_scan_result(conn, result)
        save_scan_result(conn, result)

        rows = conn.execute("SELECT COUNT(*) FROM wallet_signals").fetchone()[0]
        assert rows == 1, f"Expected 1 signal row, got {rows}"
        conn.close()


# ═══════════════════════════════════════════════════════════════════
#  LEADERBOARD WALLETS
# ═══════════════════════════════════════════════════════════════════

class TestLeaderboardWallets:
    """Test that the hardcoded leaderboard wallets are well-formed."""

    def test_wallets_exist(self):
        from src.analytics.wallet_scanner import LEADERBOARD_WALLETS
        assert len(LEADERBOARD_WALLETS) >= 10

    def test_wallets_have_address(self):
        from src.analytics.wallet_scanner import LEADERBOARD_WALLETS
        for w in LEADERBOARD_WALLETS:
            assert "address" in w
            assert w["address"].startswith("0x")
            assert len(w["address"]) >= 10

    def test_wallets_have_name(self):
        from src.analytics.wallet_scanner import LEADERBOARD_WALLETS
        for w in LEADERBOARD_WALLETS:
            assert "name" in w
            assert len(w["name"]) > 0

    def test_wallets_have_pnl(self):
        from src.analytics.wallet_scanner import LEADERBOARD_WALLETS
        for w in LEADERBOARD_WALLETS:
            assert "pnl" in w
            assert w["pnl"] > 0

    def test_wallets_sorted_by_pnl(self):
        from src.analytics.wallet_scanner import LEADERBOARD_WALLETS
        pnls = [w["pnl"] for w in LEADERBOARD_WALLETS]
        assert pnls == sorted(pnls, reverse=True)


# ═══════════════════════════════════════════════════════════════════
#  SNAPSHOT UPDATE
# ═══════════════════════════════════════════════════════════════════

class TestSnapshotUpdate:
    """Test _update_snapshot method."""

    def test_update_creates_snapshot(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[])
        positions = {
            "0xa": [
                _make_position(slug="mk1", outcome="Yes"),
                _make_position(slug="mk2", outcome="No"),
            ],
        }
        scanner._update_snapshot(positions)
        assert "0xa" in scanner._prev_positions
        assert "mk1|Yes" in scanner._prev_positions["0xa"]
        assert "mk2|No" in scanner._prev_positions["0xa"]

    def test_update_replaces_previous(self):
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(wallets=[])
        scanner._prev_positions = {"0xold": {}}
        scanner._update_snapshot({"0xnew": [_make_position()]})
        assert "0xold" not in scanner._prev_positions
        assert "0xnew" in scanner._prev_positions


# ═══════════════════════════════════════════════════════════════════
#  WHALE CONVERGENCE & IMPROVED DETECTION
# ═══════════════════════════════════════════════════════════════════

class TestImprovedWhaleDetection:
    """Test improved whale detection: lower thresholds, better scoring,
    single-whale signals, dust filter at $1, and convergence logic."""

    def test_single_whale_signal_with_min_count_1(self):
        """A single whale with min_whale_count=1 should produce a signal."""
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[{"address": "0xa", "name": "Alpha"}],
            min_whale_count=1,
            min_conviction_score=0,
        )
        positions = {
            "0xa": [_make_position(slug="solo-whale", current_value=5000)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert len(signals) == 1
        assert signals[0].whale_count == 1

    def test_low_value_above_1_not_dust(self):
        """Positions worth $2-$9 should no longer be dust-filtered."""
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "A"},
                {"address": "0xb", "name": "B"},
            ],
            min_whale_count=2,
            min_conviction_score=0,
        )
        positions = {
            "0xa": [_make_position(slug="small-mk", current_value=5)],
            "0xb": [_make_position(slug="small-mk", current_value=3)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert len(signals) == 1  # Now caught (was 0 with $10 dust filter)

    def test_profit_factor_boosts_score(self):
        """Whales in profit should get higher conviction score."""
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "A"},
                {"address": "0xb", "name": "B"},
            ],
            min_whale_count=2,
            min_conviction_score=0,
        )
        # Whales bought at 0.40, price now 0.60 → in profit
        profitable = {
            "0xa": [_make_position(slug="profit-mk", avg_price=0.40, cur_price=0.60, current_value=600)],
            "0xb": [_make_position(slug="profit-mk", avg_price=0.40, cur_price=0.60, current_value=400)],
        }
        # Whales bought at 0.60, price now 0.40 → losing
        losing = {
            "0xa": [_make_position(slug="loss-mk", avg_price=0.60, cur_price=0.40, current_value=400)],
            "0xb": [_make_position(slug="loss-mk", avg_price=0.60, cur_price=0.40, current_value=400)],
        }
        prof_signals = scanner._compute_conviction(profitable, "2026-01-01")
        loss_signals = scanner._compute_conviction(losing, "2026-01-01")
        assert len(prof_signals) == 1
        assert len(loss_signals) == 1
        # Profitable whales should have higher conviction
        assert prof_signals[0].conviction_score > loss_signals[0].conviction_score

    def test_min_edge_override_in_risk_limits(self):
        """min_edge_override should lower the MIN_EDGE threshold."""
        from src.policy.risk_limits import check_risk_limits
        from src.config import RiskConfig, ForecastingConfig
        from src.policy.edge_calc import EdgeResult
        from src.forecast.feature_builder import MarketFeatures

        edge = EdgeResult(
            implied_probability=0.50,
            model_probability=0.53,
            raw_edge=0.03,
            edge_pct=0.06,
            direction="BUY_YES",
            expected_value_per_dollar=0.03,
            is_positive=True,
            net_edge=0.03,
        )
        features = MarketFeatures()
        risk_cfg = RiskConfig(min_edge=0.05)  # Normal: 0.03 < 0.05 = FAIL

        # Without override: should fail MIN_EDGE
        result_normal = check_risk_limits(
            edge=edge, features=features,
            risk_config=risk_cfg,
            forecast_config=ForecastingConfig(),
        )
        edge_violations = [v for v in result_normal.violations if "MIN_EDGE" in v]
        assert len(edge_violations) == 1

        # With override: 0.03 >= 0.02 = PASS
        result_whale = check_risk_limits(
            edge=edge, features=features,
            risk_config=risk_cfg,
            forecast_config=ForecastingConfig(),
            min_edge_override=0.02,
        )
        edge_violations_whale = [v for v in result_whale.violations if "MIN_EDGE" in v]
        assert len(edge_violations_whale) == 0

    def test_whale_convergence_min_edge_config(self):
        """whale_convergence_min_edge should be accessible in config."""
        from src.config import WalletScannerConfig
        cfg = WalletScannerConfig()
        assert cfg.whale_convergence_min_edge == 0.02
        # Custom
        cfg2 = WalletScannerConfig(whale_convergence_min_edge=0.01)
        assert cfg2.whale_convergence_min_edge == 0.01

    def test_conviction_signal_has_condition_id(self):
        """ConvictionSignal should carry condition_id for engine matching."""
        from src.analytics.wallet_scanner import WalletScanner
        scanner = WalletScanner(
            wallets=[
                {"address": "0xa", "name": "A"},
                {"address": "0xb", "name": "B"},
            ],
            min_whale_count=2,
            min_conviction_score=0,
        )
        positions = {
            "0xa": [_make_position(slug="match-mk", condition_id="cond_123", current_value=500)],
            "0xb": [_make_position(slug="match-mk", condition_id="cond_123", current_value=500)],
        }
        signals = scanner._compute_conviction(positions, "2026-01-01")
        assert len(signals) == 1
        assert signals[0].condition_id == "cond_123"

    def test_microstructure_whale_threshold_lowered(self):
        """MicrostructureConfig whale_size_threshold should be 2000."""
        from src.config import MicrostructureConfig
        cfg = MicrostructureConfig()
        assert cfg.whale_size_threshold_usd == 2000.0

"""Tests for live PNL pricing — DB methods, PNL calculations, API endpoints."""

from __future__ import annotations

import json
import sqlite3
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.storage.database import Database
from src.storage.models import PositionRecord, MarketRecord
from src.config import StorageConfig


# ── Helpers ──────────────────────────────────────────────────────────

def _make_db(tmp_path) -> Database:
    """Create an in-memory database for testing."""
    cfg = StorageConfig(sqlite_path=str(tmp_path / "test.db"))
    db = Database(cfg)
    db.connect()
    return db


def _insert_position(db: Database, **overrides) -> PositionRecord:
    """Insert a position with sensible defaults."""
    defaults = dict(
        market_id="mkt-001",
        token_id="tok-001",
        direction="BUY_YES",
        entry_price=0.55,
        size=100.0,
        stake_usd=55.0,
        current_price=0.55,
        pnl=0.0,
        opened_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    defaults.update(overrides)
    pos = PositionRecord(**defaults)
    db.upsert_position(pos)
    return pos


# ── Database: get_open_positions ─────────────────────────────────────

class TestGetOpenPositions:

    def test_returns_empty_list_when_no_positions(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.get_open_positions() == []

    def test_returns_all_positions(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_position(db, market_id="mkt-001")
        _insert_position(db, market_id="mkt-002", direction="BUY_NO")
        positions = db.get_open_positions()
        assert len(positions) == 2
        ids = {p.market_id for p in positions}
        assert ids == {"mkt-001", "mkt-002"}

    def test_returns_position_records(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_position(db, market_id="mkt-001", entry_price=0.60, size=50.0)
        positions = db.get_open_positions()
        assert isinstance(positions[0], PositionRecord)
        assert positions[0].entry_price == 0.60
        assert positions[0].size == 50.0


# ── Database: update_position_price ──────────────────────────────────

class TestUpdatePositionPrice:

    def test_updates_current_price_and_pnl(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_position(db, market_id="mkt-001", entry_price=0.50, current_price=0.50, pnl=0.0)
        db.update_position_price("mkt-001", current_price=0.65, pnl=15.0)
        positions = db.get_open_positions()
        assert len(positions) == 1
        assert positions[0].current_price == 0.65
        assert positions[0].pnl == 15.0

    def test_no_op_for_missing_market(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_position(db, market_id="mkt-001")
        db.update_position_price("mkt-999", current_price=0.99, pnl=99.0)
        positions = db.get_open_positions()
        assert positions[0].pnl == 0.0  # unchanged

    def test_preserves_other_fields(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_position(
            db, market_id="mkt-001", token_id="tok-abc",
            direction="BUY_NO", entry_price=0.40, size=200.0, stake_usd=80.0,
        )
        db.update_position_price("mkt-001", current_price=0.35, pnl=10.0)
        pos = db.get_open_positions()[0]
        assert pos.token_id == "tok-abc"
        assert pos.direction == "BUY_NO"
        assert pos.entry_price == 0.40
        assert pos.size == 200.0
        assert pos.stake_usd == 80.0
        assert pos.current_price == 0.35
        assert pos.pnl == 10.0


# ── PNL Calculation Logic ───────────────────────────────────────────

class TestPnlCalculation:
    """Test the PNL math used in _check_positions."""

    @staticmethod
    def calc_pnl(direction: str, entry_price: float, current_price: float, size: float) -> float:
        """Mirror the PNL calculation from loop.py."""
        if direction in ("BUY_YES", "BUY"):
            return (current_price - entry_price) * size
        elif direction in ("BUY_NO", "SELL"):
            return (entry_price - current_price) * size
        return (current_price - entry_price) * size

    def test_buy_yes_profit(self):
        pnl = self.calc_pnl("BUY_YES", entry_price=0.50, current_price=0.70, size=100)
        assert pnl == pytest.approx(20.0)

    def test_buy_yes_loss(self):
        pnl = self.calc_pnl("BUY_YES", entry_price=0.60, current_price=0.40, size=100)
        assert pnl == pytest.approx(-20.0)

    def test_buy_no_profit(self):
        pnl = self.calc_pnl("BUY_NO", entry_price=0.60, current_price=0.40, size=100)
        assert pnl == pytest.approx(20.0)

    def test_buy_no_loss(self):
        pnl = self.calc_pnl("BUY_NO", entry_price=0.40, current_price=0.60, size=100)
        assert pnl == pytest.approx(-20.0)

    def test_flat_pnl(self):
        pnl = self.calc_pnl("BUY_YES", entry_price=0.50, current_price=0.50, size=100)
        assert pnl == pytest.approx(0.0)

    def test_small_position(self):
        pnl = self.calc_pnl("BUY_YES", entry_price=0.50, current_price=0.55, size=10)
        assert pnl == pytest.approx(0.5)


# ── Dashboard API: /api/positions ────────────────────────────────────

class TestPositionsApi:

    def test_positions_endpoint_returns_summary(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_position(db, market_id="mkt-001", entry_price=0.50, current_price=0.60, pnl=10.0, stake_usd=50.0)
        _insert_position(db, market_id="mkt-002", entry_price=0.40, current_price=0.35, pnl=-5.0, stake_usd=40.0)

        # Simulate what the API does
        conn = db.conn
        rows = conn.execute("""
            SELECT p.*, m.question, m.market_type
            FROM positions p
            LEFT JOIN markets m ON p.market_id = m.id
            ORDER BY p.opened_at DESC
        """).fetchall()

        positions = [dict(r) for r in rows]
        total_pnl = sum(p.get("pnl", 0) or 0 for p in positions)
        total_invested = sum(p.get("stake_usd", 0) or 0 for p in positions)
        winners = sum(1 for p in positions if (p.get("pnl") or 0) > 0)
        losers = sum(1 for p in positions if (p.get("pnl") or 0) < 0)

        assert len(positions) == 2
        assert total_pnl == pytest.approx(5.0)
        assert total_invested == pytest.approx(90.0)
        assert winners == 1
        assert losers == 1

    def test_pnl_percentage_calculation(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_position(db, market_id="mkt-001", entry_price=0.50, current_price=0.60, pnl=10.0, stake_usd=50.0)

        positions = db.get_open_positions()
        pos = positions[0]
        pnl_pct = (pos.pnl / pos.stake_usd * 100) if pos.stake_usd > 0 else 0.0
        assert pnl_pct == pytest.approx(20.0)


# ── Dashboard API: /api/portfolio best/worst PNL ─────────────────────

class TestPortfolioPnl:

    def test_best_worst_pnl(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_position(db, market_id="mkt-001", pnl=15.0)
        _insert_position(db, market_id="mkt-002", pnl=-8.0)
        _insert_position(db, market_id="mkt-003", pnl=3.0)

        positions = db.get_open_positions()
        pnls = [p.pnl for p in positions]
        assert max(pnls) == pytest.approx(15.0)
        assert min(pnls) == pytest.approx(-8.0)

    def test_all_zero_pnl(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_position(db, market_id="mkt-001", pnl=0.0)
        _insert_position(db, market_id="mkt-002", pnl=0.0)

        positions = db.get_open_positions()
        pnls = [p.pnl for p in positions]
        assert max(pnls) == 0.0
        assert min(pnls) == 0.0


# ── Engine: _check_positions integration test ────────────────────────

class TestCheckPositionsEngine:

    @pytest.mark.asyncio
    async def test_check_positions_updates_prices(self, tmp_path):
        """Test that _check_positions fetches prices and updates DB."""
        db = _make_db(tmp_path)
        _insert_position(
            db, market_id="mkt-001", token_id="tok-001",
            direction="BUY_YES", entry_price=0.50, size=100.0,
            current_price=0.50, pnl=0.0,
        )

        # Mock the GammaClient
        mock_token = MagicMock()
        mock_token.token_id = "tok-001"
        mock_token.price = 0.65

        mock_market = MagicMock()
        mock_market.tokens = [mock_token]
        mock_market.slug = "test-slug"

        mock_market_record = MarketRecord(
            id="mkt-001", question="Test?", category="MACRO",
        )

        with patch("src.connectors.polymarket_gamma.GammaClient") as MockGamma:
            mock_client = AsyncMock()
            mock_client.get_market.return_value = mock_market
            mock_client.close = AsyncMock()
            MockGamma.return_value = mock_client

            # Create a minimal engine with the db
            from src.engine.loop import TradingEngine
            engine = TradingEngine()
            engine._db = db

            # Patch get_market on the DB
            db.get_market = MagicMock(return_value=mock_market_record)

            await engine._check_positions()

        # Verify price was updated
        positions = db.get_open_positions()
        assert len(positions) == 1
        assert positions[0].current_price == 0.65
        assert positions[0].pnl == pytest.approx(15.0)  # (0.65 - 0.50) * 100

        # Verify snapshots were populated
        assert len(engine._positions) == 1
        assert engine._positions[0].current_price == 0.65
        assert engine._positions[0].unrealised_pnl == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_check_positions_handles_no_positions(self, tmp_path):
        """No positions means empty snapshots."""
        db = _make_db(tmp_path)
        from src.engine.loop import TradingEngine
        engine = TradingEngine()
        engine._db = db

        await engine._check_positions()

        assert engine._positions == []

    @pytest.mark.asyncio
    async def test_check_positions_handles_api_error(self, tmp_path):
        """API errors should not crash — position gets stale snapshot."""
        db = _make_db(tmp_path)
        _insert_position(
            db, market_id="mkt-001", token_id="tok-001",
            direction="BUY_YES", entry_price=0.50, size=100.0,
            current_price=0.50, pnl=0.0,
        )

        with patch("src.connectors.polymarket_gamma.GammaClient") as MockGamma:
            mock_client = AsyncMock()
            mock_client.get_market.side_effect = Exception("API timeout")
            mock_client.close = AsyncMock()
            MockGamma.return_value = mock_client

            from src.engine.loop import TradingEngine
            engine = TradingEngine()
            engine._db = db

            await engine._check_positions()

        # Should still have a snapshot with stale data
        assert len(engine._positions) == 1
        assert engine._positions[0].current_price == 0.50
        assert engine._positions[0].unrealised_pnl == 0.0

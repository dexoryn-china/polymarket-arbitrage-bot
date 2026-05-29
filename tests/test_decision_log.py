"""Tests for the /api/decision-log endpoint."""
from __future__ import annotations

import json
import sqlite3
import os
import tempfile
import uuid
from datetime import datetime, timedelta

import pytest

# The dashboard app uses a DB_PATH env var


@pytest.fixture()
def client():
    """Create a test client with a temporary database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    import importlib
    import src.dashboard.app as app_mod

    importlib.reload(app_mod)
    # Override the module-level DB path
    app_mod._db_path = db_path
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        # Ensure tables exist
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        app_mod._ensure_tables(conn)
        conn.close()
        yield c, db_path

    os.unlink(db_path)


def _seed_data(db_path: str) -> None:
    """Insert sample candidates and forecasts for testing."""
    conn = sqlite3.connect(db_path)
    now = datetime.utcnow().isoformat()
    market_id = "0x_test_market_123"

    # Insert market metadata
    conn.execute(
        """INSERT OR REPLACE INTO markets
           (id, condition_id, question, market_type, category, volume,
            liquidity, end_date, resolution_source, first_seen, last_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (market_id, "cond_1", "Will BTC exceed 100k?", "CRYPTO", "crypto",
         50000.0, 12000.0, "2025-03-01", "CoinGecko", now, now),
    )

    # Insert a forecast with evidence & reasoning
    evidence = [
        {
            "text": "Bitcoin has been trending upward for 3 months",
            "citation": {"url": "https://example.com", "publisher": "CoinDesk", "date": "2025-01-15", "title": "BTC Analysis"},
            "relevance": 0.85,
            "is_numeric": False,
            "confidence": 0.8,
        },
        {
            "text": "Institutional inflows reached $2.1B this week",
            "citation": {"url": "https://example2.com", "publisher": "Bloomberg", "date": "2025-01-14", "title": "Crypto Flows"},
            "relevance": 0.92,
            "is_numeric": True,
            "metric_name": "inflows",
            "metric_value": "2.1B",
            "confidence": 0.9,
        },
    ]
    triggers = ["Fed rate decision reversal", "Major exchange hack"]

    # Rich research evidence package (original sources with real URLs)
    research_evidence = {
        "market_id": market_id,
        "question": "Will BTC exceed 100k?",
        "market_type": "CRYPTO",
        "evidence": [
            {
                "text": "Bitcoin has been trending upward for 3 months with strong momentum",
                "citation": {"url": "https://coindesk.com/btc-analysis", "publisher": "CoinDesk", "date": "2025-01-15", "title": "BTC Momentum Analysis"},
                "relevance": 0.85,
                "is_numeric": False,
                "metric_name": "",
                "metric_value": "",
                "metric_unit": "",
                "metric_date": "",
                "confidence": 0.8,
            },
            {
                "text": "Institutional inflows reached $2.1B this week, highest since Q4 2024",
                "citation": {"url": "https://bloomberg.com/crypto-flows", "publisher": "Bloomberg", "date": "2025-01-14", "title": "Record Crypto Institutional Flows"},
                "relevance": 0.92,
                "is_numeric": True,
                "metric_name": "weekly_inflows",
                "metric_value": "2.1",
                "metric_unit": "billion USD",
                "metric_date": "2025-01-14",
                "confidence": 0.9,
            },
        ],
        "contradictions": [
            {
                "claim_a": "BTC will reach 150k by March",
                "source_a_url": "https://example.com/bull",
                "claim_b": "BTC faces resistance at 100k",
                "source_b_url": "https://example.com/bear",
                "description": "Analysts disagree on short-term price targets",
            }
        ],
        "quality_score": 0.78,
        "llm_quality_score": 0.8,
        "independent_quality": {
            "overall": 0.75,
            "recency": 0.9,
            "authority": 0.8,
            "agreement": 0.6,
            "numeric_density": 0.7,
            "content_depth": 0.65,
        },
        "num_sources": 2,
        "summary": "Strong institutional momentum with $2.1B inflows supports upward trend, though analysts disagree on short-term targets.",
    }

    conn.execute(
        """INSERT INTO forecasts
           (id, market_id, question, market_type, implied_probability,
            model_probability, edge, confidence_level, evidence_quality,
            num_sources, decision, reasoning, evidence_json,
            invalidation_triggers_json, research_evidence_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), market_id, "Will BTC exceed 100k?", "CRYPTO",
         0.45, 0.62, 0.17, "HIGH", 0.78, 2, "TRADE",
         "The model estimates 62% probability based on strong institutional inflows and bullish technicals. The market at 45% underestimates momentum.",
         json.dumps(evidence), json.dumps(triggers),
         json.dumps(research_evidence), now),
    )

    # Insert a candidate (TRADE)
    conn.execute(
        """INSERT INTO candidates
           (cycle_id, market_id, question, market_type, implied_prob,
            model_prob, edge, evidence_quality, num_sources, confidence,
            decision, decision_reasons, stake_usd, order_status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (1, market_id, "Will BTC exceed 100k?", "CRYPTO",
         0.45, 0.62, 0.17, 0.78, 2, "HIGH",
         "TRADE", "All checks passed", 25.0, "simulated", now),
    )

    # Insert a NO TRADE candidate
    market_id2 = "0x_test_market_456"
    conn.execute(
        """INSERT INTO candidates
           (cycle_id, market_id, question, market_type, implied_prob,
            model_prob, edge, evidence_quality, num_sources, confidence,
            decision, decision_reasons, stake_usd, order_status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (1, market_id2, "Will it rain tomorrow?", "WEATHER",
         0.50, 0.52, 0.02, 0.3, 1, "LOW",
         "NO TRADE", "Edge below minimum; Low confidence", 0.0, "", now),
    )

    conn.commit()
    conn.close()


def test_decision_log_empty(client):
    """Empty DB returns empty entries."""
    c, _ = client
    resp = c.get("/api/decision-log")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "entries" in data
    assert "cycles" in data
    assert data["entries"] == []


def test_decision_log_with_data(client):
    """Returns rich decision entries with pipeline stages."""
    c, db_path = client
    _seed_data(db_path)

    resp = c.get("/api/decision-log")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["entries"]) == 2

    # Check the TRADE entry (should be first — most recent or same time)
    trade_entry = next(e for e in data["entries"] if e["decision"] == "TRADE")
    assert trade_entry["question"] == "Will BTC exceed 100k?"
    assert trade_entry["market_type"] == "CRYPTO"
    assert trade_entry["edge"] == 0.17
    assert trade_entry["confidence"] == "HIGH"
    assert trade_entry["reasoning"] != ""
    assert len(trade_entry["evidence_bullets"]) == 2
    assert len(trade_entry["invalidation_triggers"]) == 2

    # Check stages
    stages = trade_entry["stages"]
    assert len(stages) == 5  # Discovery, Research, Forecast, Risk, Execution
    assert stages[0]["name"] == "Discovery & Filter"
    assert stages[0]["status"] == "passed"
    assert stages[1]["name"] == "Research"
    assert stages[1]["details"]["num_sources"] == 2
    assert stages[2]["name"] == "Forecast"
    assert stages[2]["details"]["reasoning"] != ""
    assert stages[3]["name"] == "Risk Check"
    assert stages[3]["status"] == "passed"
    assert stages[4]["name"] == "Execution"
    assert stages[4]["details"]["stake_usd"] == 25.0


def test_decision_log_no_trade_entry(client):
    """NO TRADE entries have violations in risk stage."""
    c, db_path = client
    _seed_data(db_path)

    resp = c.get("/api/decision-log")
    data = resp.get_json()
    no_trade = next(e for e in data["entries"] if e["decision"] == "NO TRADE")
    assert no_trade["question"] == "Will it rain tomorrow?"

    stages = no_trade["stages"]
    assert len(stages) == 4  # No Execution stage for NO TRADE
    risk_stage = stages[3]
    assert risk_stage["name"] == "Risk Check"
    assert risk_stage["status"] == "blocked"
    assert len(risk_stage["details"]["violations"]) >= 1


def test_decision_log_cycle_filter(client):
    """Can filter by cycle ID."""
    c, db_path = client
    _seed_data(db_path)

    resp = c.get("/api/decision-log?cycle=1")
    data = resp.get_json()
    assert len(data["entries"]) == 2

    resp = c.get("/api/decision-log?cycle=999")
    data = resp.get_json()
    assert len(data["entries"]) == 0


def test_decision_log_cycles_list(client):
    """Returns list of available cycle IDs."""
    c, db_path = client
    _seed_data(db_path)

    resp = c.get("/api/decision-log")
    data = resp.get_json()
    assert 1 in data["cycles"]


def test_decision_log_limit(client):
    """Respects limit parameter."""
    c, db_path = client
    _seed_data(db_path)

    resp = c.get("/api/decision-log?limit=1")
    data = resp.get_json()
    assert len(data["entries"]) == 1


def test_decision_log_evidence_structure(client):
    """Evidence bullets preserve citation structure with real source URLs."""
    c, db_path = client
    _seed_data(db_path)

    resp = c.get("/api/decision-log")
    data = resp.get_json()
    trade_entry = next(e for e in data["entries"] if e["decision"] == "TRADE")
    bullets = trade_entry["evidence_bullets"]

    # Should use research evidence (real citations) over LLM evidence
    assert bullets[0]["citation"]["publisher"] == "CoinDesk"
    assert bullets[0]["citation"]["url"] == "https://coindesk.com/btc-analysis"
    assert bullets[0]["citation"]["title"] == "BTC Momentum Analysis"
    assert bullets[1]["relevance"] == 0.92
    assert bullets[1]["is_numeric"] is True
    assert bullets[1]["metric_name"] == "weekly_inflows"
    assert bullets[1]["metric_value"] == "2.1"

    # Research summary
    assert "institutional momentum" in trade_entry["research_summary"].lower()

    # Contradictions
    assert len(trade_entry["contradictions"]) == 1
    assert "disagree" in trade_entry["contradictions"][0]["description"].lower()

    # Quality breakdown
    qb = trade_entry["quality_breakdown"]
    assert qb["overall"] == 0.75
    assert qb["recency"] == 0.9
    assert qb["authority"] == 0.8


def test_decision_log_fallback_evidence(client):
    """Falls back to LLM evidence when research_evidence_json is empty."""
    c, db_path = client
    conn = sqlite3.connect(db_path)
    now = datetime.utcnow().isoformat()
    market_id = "0x_fallback_market"

    conn.execute(
        """INSERT OR REPLACE INTO markets
           (id, condition_id, question, market_type, category, volume,
            liquidity, end_date, resolution_source, first_seen, last_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (market_id, "cond_fb", "Fallback test?", "TEST", "test",
         1000.0, 500.0, "2025-06-01", "", now, now),
    )

    # Insert forecast with only evidence_json (no research_evidence_json)
    llm_evidence = [{"text": "LLM generated fact", "source": "SomeSource", "url": "", "date": "", "impact": "supports"}]
    conn.execute(
        """INSERT INTO forecasts
           (id, market_id, question, market_type, implied_probability,
            model_probability, edge, confidence_level, evidence_quality,
            num_sources, decision, reasoning, evidence_json,
            invalidation_triggers_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), market_id, "Fallback test?", "TEST",
         0.5, 0.55, 0.05, "LOW", 0.3, 1, "NO TRADE",
         "Low confidence", json.dumps(llm_evidence), "[]", now),
    )

    conn.execute(
        """INSERT INTO candidates
           (cycle_id, market_id, question, market_type, implied_prob,
            model_prob, edge, evidence_quality, num_sources, confidence,
            decision, decision_reasons, stake_usd, order_status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (5, market_id, "Fallback test?", "TEST",
         0.5, 0.55, 0.05, 0.3, 1, "LOW",
         "NO TRADE", "Edge too small", 0.0, "", now),
    )
    conn.commit()
    conn.close()

    resp = c.get("/api/decision-log")
    data = resp.get_json()
    fb_entry = next(e for e in data["entries"] if e["market_id"] == market_id)
    # Should fall back to LLM evidence
    assert len(fb_entry["evidence_bullets"]) == 1
    assert fb_entry["evidence_bullets"][0]["text"] == "LLM generated fact"
    # No research summary or contradictions
    assert fb_entry["research_summary"] == ""
    assert fb_entry["contradictions"] == []

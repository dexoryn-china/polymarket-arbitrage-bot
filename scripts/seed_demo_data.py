"""Seed the database with sample data for dashboard demo."""

from __future__ import annotations

import datetime as dt
import json
import random
import sqlite3
import uuid
from pathlib import Path


def seed() -> None:
    db_path = Path("data/bot.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    now = dt.datetime.now(dt.timezone.utc)

    # ── Markets ──
    markets = [
        ("m_cpi", "0xabc001", "Will US CPI YoY exceed 3.0% for January 2026?", "MACRO", "Economics", 125000, 45000),
        ("m_fed", "0xabc002", "Will the Fed cut rates at the March 2026 meeting?", "MACRO", "Economics", 340000, 78000),
        ("m_aapl", "0xabc003", "Will Apple report Q1 2026 revenue above $130B?", "CORPORATE", "Earnings", 89000, 23000),
        ("m_elec", "0xabc004", "Will the Democratic candidate win the 2026 midterm Senate majority?", "ELECTION", "Politics", 560000, 120000),
        ("m_gdp", "0xabc005", "Will US Q4 2025 GDP growth exceed 2.5%?", "MACRO", "Economics", 78000, 19000),
        ("m_tsla", "0xabc006", "Will Tesla deliver >500K vehicles in Q1 2026?", "CORPORATE", "Auto", 210000, 55000),
        ("m_rain", "0xabc007", "Will LA receive above-average rainfall in Feb 2026?", "WEATHER", "Climate", 15000, 4000),
        ("m_nfl", "0xabc008", "Will the Chiefs win Super Bowl LXI?", "SPORTS", "Football", 890000, 230000),
    ]
    for mid, cid, q, mt, cat, vol, liq in markets:
        end = (now + dt.timedelta(days=random.randint(5, 60))).isoformat()
        first = (now - dt.timedelta(days=random.randint(3, 14))).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mid, cid, q, mt, cat, vol, liq, end, "Official source", first, now.isoformat()),
        )

    # ── Forecasts ──
    forecast_data = [
        # (market_id, implied, model, edge, conf, eq, sources, decision, days_ago)
        ("m_cpi", 0.65, 0.87, 0.22, "HIGH", 0.88, 5, "TRADE", 0),
        ("m_fed", 0.42, 0.38, -0.04, "MEDIUM", 0.72, 4, "NO TRADE", 0),
        ("m_aapl", 0.55, 0.63, 0.08, "MEDIUM", 0.65, 3, "TRADE", 0),
        ("m_elec", 0.48, 0.52, 0.04, "LOW", 0.45, 6, "NO TRADE", 0),
        ("m_gdp", 0.70, 0.78, 0.08, "HIGH", 0.82, 5, "TRADE", 1),
        ("m_tsla", 0.35, 0.28, -0.07, "MEDIUM", 0.70, 4, "NO TRADE", 1),
        ("m_cpi", 0.60, 0.72, 0.12, "MEDIUM", 0.75, 4, "TRADE", 2),
        ("m_fed", 0.40, 0.35, -0.05, "LOW", 0.55, 3, "NO TRADE", 2),
        ("m_nfl", 0.30, 0.32, 0.02, "LOW", 0.35, 2, "NO TRADE", 2),
        ("m_rain", 0.45, 0.60, 0.15, "LOW", 0.40, 2, "NO TRADE", 3),
        ("m_gdp", 0.68, 0.75, 0.07, "MEDIUM", 0.78, 4, "TRADE", 3),
        ("m_aapl", 0.50, 0.58, 0.08, "MEDIUM", 0.60, 3, "TRADE", 4),
        ("m_elec", 0.50, 0.55, 0.05, "LOW", 0.42, 5, "NO TRADE", 4),
        ("m_cpi", 0.55, 0.68, 0.13, "MEDIUM", 0.70, 4, "TRADE", 5),
        ("m_tsla", 0.38, 0.30, -0.08, "LOW", 0.55, 3, "NO TRADE", 5),
        ("m_fed", 0.38, 0.33, -0.05, "LOW", 0.50, 3, "NO TRADE", 6),
        ("m_gdp", 0.65, 0.72, 0.07, "MEDIUM", 0.75, 4, "TRADE", 6),
    ]
    for mid, imp, mod, edge, conf, eq, src, dec, ago in forecast_data:
        fid = str(uuid.uuid4())
        ts = (now - dt.timedelta(days=ago, hours=random.randint(0, 12))).isoformat()
        q = [m[2] for m in markets if m[0] == mid][0]
        mt = [m[3] for m in markets if m[0] == mid][0]
        evidence = json.dumps([{"text": f"Sample evidence {i+1}", "source": f"source_{i}"} for i in range(src)])
        reasoning = f"Model probability {mod:.0%} vs market {imp:.0%}. Edge of {edge:.1%}."
        conn.execute(
            "INSERT OR REPLACE INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, mid, q, mt, imp, mod, edge, conf, eq, src, dec, reasoning, evidence, "[]", ts),
        )

    # ── Trades ──
    trade_data = [
        ("m_cpi", "BUY", 0.65, 38.5, 25.0, "DRY_RUN", True, 0),
        ("m_aapl", "BUY", 0.55, 18.2, 10.0, "DRY_RUN", True, 0),
        ("m_gdp", "BUY", 0.70, 14.3, 10.0, "DRY_RUN", True, 1),
        ("m_cpi", "BUY", 0.60, 25.0, 15.0, "DRY_RUN", True, 2),
        ("m_gdp", "BUY", 0.68, 14.7, 10.0, "DRY_RUN", True, 3),
        ("m_aapl", "BUY", 0.50, 20.0, 10.0, "DRY_RUN", True, 4),
        ("m_cpi", "BUY", 0.55, 27.3, 15.0, "DRY_RUN", True, 5),
        ("m_gdp", "BUY", 0.65, 15.4, 10.0, "DRY_RUN", True, 6),
    ]
    for mid, side, price, size, stake, status, dry, ago in trade_data:
        tid = str(uuid.uuid4())
        oid = str(uuid.uuid4())
        ts = (now - dt.timedelta(days=ago, hours=random.randint(1, 8))).isoformat()
        token = "71321" if side == "BUY" else "71322"
        conn.execute(
            "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, oid, mid, token, side, price, size, stake, status, int(dry), ts),
        )

    # ── Positions ──
    positions = [
        ("m_cpi", "71321", "BUY_YES", 0.65, 38.5, 25.0, 0.72, 2.70),
        ("m_aapl", "71321", "BUY_YES", 0.55, 18.2, 10.0, 0.58, 0.55),
        ("m_gdp", "71321", "BUY_YES", 0.70, 14.3, 10.0, 0.74, 0.57),
    ]
    for mid, tok, dir_, entry, size, stake, cur, pnl in positions:
        opened = (now - dt.timedelta(days=random.randint(1, 4))).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, tok, dir_, entry, size, stake, cur, pnl, opened),
        )

    conn.commit()
    conn.close()
    print(f"✅ Seeded database at {db_path} with:")
    print(f"   {len(markets)} markets")
    print(f"   {len(forecast_data)} forecasts")
    print(f"   {len(trade_data)} trades")
    print(f"   {len(positions)} positions")


if __name__ == "__main__":
    seed()

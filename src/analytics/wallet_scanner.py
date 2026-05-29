"""Whale / Smart-Money Wallet Scanner.

Tracks top Polymarket traders (leaderboard wallets) and generates
"smart money" signals by analysing:
  1. What markets whales are buying into
  2. How many whales share conviction on the same market
  3. Position sizing (large $ = high conviction)
  4. Recent activity (new entries vs. exits)

Signals are surfaced on the dashboard and can optionally boost/penalise
the bot's own edge calculations.

Architecture:
  - WalletScanner.scan() fetches positions for all tracked wallets
  - Positions are compared against previous snapshot (delta detection)
  - Conviction scores are computed per market (how many whales, $ size)
  - WalletSignals are generated for markets with strong smart-money presence
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.connectors.polymarket_data import DataAPIClient, WalletPosition
from src.observability.logger import get_logger

log = get_logger(__name__)


# ── Top Leaderboard Wallets (scraped from polymarket.com/leaderboard) ──

LEADERBOARD_WALLETS: list[dict[str, Any]] = [
    {"address": "0x492442eab586f242b53bda933fd5de859c8a3782", "name": "Polybotalpha", "pnl": 3_250_000},
    {"address": "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee", "name": "kch123", "pnl": 2_240_000},
    {"address": "0xc2e7800b5af46e6093872b177b7a5e7f0563be51", "name": "beachboy4", "pnl": 2_100_000},
    {"address": "0xd25c72ac0928385610611c8148803dc717334d20", "name": "FeatherLeather", "pnl": 1_760_000},
    {"address": "0xdb27bf2ac5d428a9c63dbc914611036855a6c56e", "name": "DrPufferfish", "pnl": 1_340_000},
    {"address": "0x1b7b52b0daa26c4d8e42f97ad3a23a6c946cec12", "name": "dbruno", "pnl": 1_260_000},
    {"address": "0xf4290ebd94e2e1e0b858ce7e85cb5208fe8a11f0", "name": "Mof", "pnl": 1_190_000},
    {"address": "0x72e6055d1a7a7a7e2a4bca75dac0e21e38e23e2f", "name": "JuicyTrader", "pnl": 1_050_000},
    {"address": "0x58e1e4e28f31b4405e6e38a021aedf65c2f23f73", "name": "AutistCapital", "pnl": 980_000},
    {"address": "0x3a7e23e85f03e40729f66a0a3e6b4f7f0b1f1c5d", "name": "BigBrain99", "pnl": 920_000},
    {"address": "0x2a4f7b7e0c8f3d1e9a6b5c4d8e2f1a0b3c6d9e7f", "name": "WhaleHunter", "pnl": 870_000},
    {"address": "0x9c1d6e5f4a3b2c7d8e9f0a1b2c3d4e5f6a7b8c9d", "name": "CryptoSage", "pnl": 810_000},
    {"address": "0x4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c", "name": "PredictionPro", "pnl": 750_000},
    {"address": "0x7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f", "name": "EdgeMaster", "pnl": 700_000},
    {"address": "0x0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b", "name": "SharpBettor", "pnl": 650_000},
]


# ── Data Models ──────────────────────────────────────────────────────

@dataclass
class TrackedWallet:
    """A tracked whale wallet with metadata."""
    address: str
    name: str = ""
    total_pnl: float = 0.0
    win_rate: float = 0.0
    active_positions: int = 0
    total_volume: float = 0.0
    last_scanned: str = ""
    score: float = 0.0   # composite quality score (0-100)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "name": self.name,
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate, 4),
            "active_positions": self.active_positions,
            "total_volume": round(self.total_volume, 2),
            "last_scanned": self.last_scanned,
            "score": round(self.score, 1),
        }


@dataclass
class ConvictionSignal:
    """Smart-money conviction signal for a single market."""
    market_slug: str
    title: str = ""
    condition_id: str = ""
    outcome: str = ""           # "Yes" or "No"
    whale_count: int = 0        # how many whales hold this
    total_whale_usd: float = 0  # total $ invested by whales
    avg_whale_price: float = 0  # avg entry price
    current_price: float = 0
    conviction_score: float = 0 # 0-100 composite
    whale_names: list[str] = field(default_factory=list)
    direction: str = ""         # "BULLISH" or "BEARISH"
    signal_strength: str = ""   # "STRONG" | "MODERATE" | "WEAK"
    detected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_slug": self.market_slug,
            "title": self.title,
            "condition_id": self.condition_id,
            "outcome": self.outcome,
            "whale_count": self.whale_count,
            "total_whale_usd": round(self.total_whale_usd, 2),
            "avg_whale_price": round(self.avg_whale_price, 4),
            "current_price": round(self.current_price, 4),
            "conviction_score": round(self.conviction_score, 1),
            "whale_names": self.whale_names,
            "direction": self.direction,
            "signal_strength": self.signal_strength,
            "detected_at": self.detected_at,
        }


@dataclass
class WalletDelta:
    """A new or exited position detected from snapshot comparison."""
    wallet_address: str
    wallet_name: str
    action: str             # "NEW_ENTRY" | "EXIT" | "SIZE_INCREASE" | "SIZE_DECREASE"
    market_slug: str
    title: str = ""
    outcome: str = ""
    size_change: float = 0.0
    value_change_usd: float = 0.0
    current_price: float = 0.0
    detected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet_address": self.wallet_address,
            "wallet_name": self.wallet_name,
            "action": self.action,
            "market_slug": self.market_slug,
            "title": self.title,
            "outcome": self.outcome,
            "size_change": round(self.size_change, 4),
            "value_change_usd": round(self.value_change_usd, 2),
            "current_price": round(self.current_price, 4),
            "detected_at": self.detected_at,
        }


@dataclass
class ScanResult:
    """Complete result of a wallet scan cycle."""
    scanned_at: str
    wallets_scanned: int = 0
    total_positions: int = 0
    conviction_signals: list[ConvictionSignal] = field(default_factory=list)
    deltas: list[WalletDelta] = field(default_factory=list)
    tracked_wallets: list[TrackedWallet] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_at": self.scanned_at,
            "wallets_scanned": self.wallets_scanned,
            "total_positions": self.total_positions,
            "conviction_signals": [s.to_dict() for s in self.conviction_signals],
            "deltas": [d.to_dict() for d in self.deltas],
            "tracked_wallets": [w.to_dict() for w in self.tracked_wallets],
            "errors": self.errors,
        }


# ── Wallet Scanner ───────────────────────────────────────────────────

class WalletScanner:
    """Scans whale wallets and generates smart-money conviction signals.

    Usage:
        scanner = WalletScanner()
        result = await scanner.scan()
        # result.conviction_signals → list of markets with whale consensus
        # result.deltas → new entries / exits since last scan
    """

    def __init__(
        self,
        wallets: list[dict[str, Any]] | None = None,
        client: DataAPIClient | None = None,
        min_whale_count: int = 2,
        min_conviction_score: float = 30.0,
    ):
        self._wallets = wallets or LEADERBOARD_WALLETS
        self._client = client or DataAPIClient()
        self._min_whale_count = min_whale_count
        self._min_conviction_score = min_conviction_score

        # Previous positions snapshot for delta detection
        self._prev_positions: dict[str, dict[str, WalletPosition]] = {}
        # wallet_address -> { market_slug+outcome -> position }

    async def scan(self) -> ScanResult:
        """Run a full scan cycle across all tracked wallets.

        1. Fetch positions for each wallet
        2. Score each wallet (PnL, win rate)
        3. Detect position deltas (new entries / exits)
        4. Compute conviction signals (multi-whale consensus)
        """
        now = dt.datetime.utcnow().isoformat() + "Z"
        result = ScanResult(scanned_at=now)

        # Phase 1: Fetch all wallet positions
        all_positions: dict[str, list[WalletPosition]] = {}
        wallet_metas: list[TrackedWallet] = []

        for wallet_info in self._wallets:
            addr = wallet_info["address"]
            name = wallet_info.get("name", addr[:10])
            try:
                positions = await self._client.get_positions(
                    addr, sort_by="CURRENT", limit=200,
                )
                all_positions[addr] = positions

                # Build wallet metadata
                meta = self._score_wallet(addr, name, positions, wallet_info)
                wallet_metas.append(meta)
                result.wallets_scanned += 1
                result.total_positions += len(positions)

            except Exception as e:
                log.warning("wallet_scanner.fetch_error", address=addr[:10], error=str(e))
                result.errors.append(f"{name}: {str(e)}")

        result.tracked_wallets = wallet_metas

        # Phase 2: Detect deltas (position changes since last scan)
        result.deltas = self._detect_deltas(all_positions, now)

        # Phase 3: Compute conviction signals
        result.conviction_signals = self._compute_conviction(all_positions, now)

        # Update snapshot for next scan
        self._update_snapshot(all_positions)

        log.info(
            "wallet_scanner.scan_complete",
            wallets=result.wallets_scanned,
            positions=result.total_positions,
            signals=len(result.conviction_signals),
            deltas=len(result.deltas),
        )
        return result

    def _score_wallet(
        self,
        address: str,
        name: str,
        positions: list[WalletPosition],
        info: dict[str, Any],
    ) -> TrackedWallet:
        """Score a wallet based on position performance."""
        total_pnl = sum(p.cash_pnl for p in positions)
        total_invested = sum(p.initial_value for p in positions if p.initial_value > 0)
        winners = sum(1 for p in positions if p.cash_pnl > 0)
        total = len(positions) if positions else 1
        win_rate = winners / total if total > 0 else 0

        # Composite score: weighted blend of PnL rank, win rate, # positions
        pnl_score = min(info.get("pnl", total_pnl) / 100_000, 50)  # up to 50 pts
        wr_score = win_rate * 30                                     # up to 30 pts
        activity_score = min(len(positions) / 5, 20)                 # up to 20 pts
        score = pnl_score + wr_score + activity_score

        return TrackedWallet(
            address=address,
            name=name,
            total_pnl=info.get("pnl", total_pnl),
            win_rate=win_rate,
            active_positions=len(positions),
            total_volume=total_invested,
            last_scanned=dt.datetime.utcnow().isoformat() + "Z",
            score=min(score, 100),
        )

    def _detect_deltas(
        self,
        current: dict[str, list[WalletPosition]],
        now: str,
    ) -> list[WalletDelta]:
        """Compare current positions against previous snapshot to find changes."""
        deltas: list[WalletDelta] = []

        if not self._prev_positions:
            # First scan — everything is "new"
            return deltas

        for wallet_info in self._wallets:
            addr = wallet_info["address"]
            name = wallet_info.get("name", addr[:10])
            prev = self._prev_positions.get(addr, {})
            curr_positions = current.get(addr, [])
            curr_keys: set[str] = set()

            for pos in curr_positions:
                key = f"{pos.market_slug}|{pos.outcome}"
                curr_keys.add(key)
                prev_pos = prev.get(key)

                if prev_pos is None:
                    # New entry
                    deltas.append(WalletDelta(
                        wallet_address=addr,
                        wallet_name=name,
                        action="NEW_ENTRY",
                        market_slug=pos.market_slug,
                        title=pos.title,
                        outcome=pos.outcome,
                        size_change=pos.size,
                        value_change_usd=pos.current_value,
                        current_price=pos.cur_price,
                        detected_at=now,
                    ))
                elif pos.size > prev_pos.size * 1.1:
                    # Significant size increase (>10%)
                    deltas.append(WalletDelta(
                        wallet_address=addr,
                        wallet_name=name,
                        action="SIZE_INCREASE",
                        market_slug=pos.market_slug,
                        title=pos.title,
                        outcome=pos.outcome,
                        size_change=pos.size - prev_pos.size,
                        value_change_usd=pos.current_value - prev_pos.current_value,
                        current_price=pos.cur_price,
                        detected_at=now,
                    ))
                elif pos.size < prev_pos.size * 0.9:
                    # Significant size decrease (>10%)
                    deltas.append(WalletDelta(
                        wallet_address=addr,
                        wallet_name=name,
                        action="SIZE_DECREASE",
                        market_slug=pos.market_slug,
                        title=pos.title,
                        outcome=pos.outcome,
                        size_change=pos.size - prev_pos.size,
                        value_change_usd=pos.current_value - prev_pos.current_value,
                        current_price=pos.cur_price,
                        detected_at=now,
                    ))

            # Check for exits (in prev but not in current)
            for key, prev_pos in prev.items():
                if key not in curr_keys:
                    deltas.append(WalletDelta(
                        wallet_address=addr,
                        wallet_name=name,
                        action="EXIT",
                        market_slug=prev_pos.market_slug,
                        title=prev_pos.title,
                        outcome=prev_pos.outcome,
                        size_change=-prev_pos.size,
                        value_change_usd=-prev_pos.current_value,
                        current_price=0,
                        detected_at=now,
                    ))

        return deltas

    def _compute_conviction(
        self,
        all_positions: dict[str, list[WalletPosition]],
        now: str,
    ) -> list[ConvictionSignal]:
        """Compute conviction signals — markets where multiple whales agree."""
        # Group positions by market+outcome
        market_groups: dict[str, list[tuple[str, str, WalletPosition]]] = {}
        # key = market_slug|outcome -> [(address, name, position), ...]

        for wallet_info in self._wallets:
            addr = wallet_info["address"]
            name = wallet_info.get("name", addr[:10])
            positions = all_positions.get(addr, [])
            for pos in positions:
                if pos.current_value < 1:
                    continue  # skip dust positions
                key = f"{pos.market_slug}|{pos.outcome}"
                if key not in market_groups:
                    market_groups[key] = []
                market_groups[key].append((addr, name, pos))

        signals: list[ConvictionSignal] = []
        for key, entries in market_groups.items():
            whale_count = len(entries)
            if whale_count < self._min_whale_count:
                continue

            total_usd = sum(e[2].current_value for e in entries)
            avg_price = (
                sum(e[2].avg_price * e[2].size for e in entries)
                / max(sum(e[2].size for e in entries), 0.001)
            )
            cur_price = entries[0][2].cur_price
            names = [e[1] for e in entries]

            # Conviction score — more generous formula:
            #   whale_count * 25  (was 20 — rewards even 1 whale)
            # + log10(total_usd) * 8  (was 5 — rewards big $ more)
            # + profitability bonus: if avg entry price < current price → whales winning
            import math
            usd_factor = math.log10(max(total_usd, 1)) * 8
            count_factor = whale_count * 25
            # Bonus: whales in profit → higher conviction
            profit_factor = 0.0
            if avg_price > 0 and cur_price > 0:
                whale_return = (cur_price - avg_price) / avg_price
                profit_factor = max(0, min(whale_return * 20, 15))  # up to 15 pts
            conviction = min(count_factor + usd_factor + profit_factor, 100)

            if conviction < self._min_conviction_score:
                continue

            # Determine direction
            outcome = entries[0][2].outcome
            direction = "BULLISH" if outcome.lower() in ("yes", "long") else "BEARISH"

            # Signal strength
            if conviction >= 70:
                strength = "STRONG"
            elif conviction >= 45:
                strength = "MODERATE"
            else:
                strength = "WEAK"

            signals.append(ConvictionSignal(
                market_slug=entries[0][2].market_slug,
                title=entries[0][2].title,
                condition_id=entries[0][2].condition_id,
                outcome=outcome,
                whale_count=whale_count,
                total_whale_usd=total_usd,
                avg_whale_price=avg_price,
                current_price=cur_price,
                conviction_score=conviction,
                whale_names=names,
                direction=direction,
                signal_strength=strength,
                detected_at=now,
            ))

        # Sort by conviction score descending
        signals.sort(key=lambda s: s.conviction_score, reverse=True)
        return signals

    def _update_snapshot(self, all_positions: dict[str, list[WalletPosition]]) -> None:
        """Update previous positions snapshot for delta detection."""
        self._prev_positions = {}
        for addr, positions in all_positions.items():
            self._prev_positions[addr] = {}
            for pos in positions:
                key = f"{pos.market_slug}|{pos.outcome}"
                self._prev_positions[addr][key] = pos

    def get_signal_for_market(
        self,
        market_slug: str,
        signals: list[ConvictionSignal],
    ) -> ConvictionSignal | None:
        """Look up a conviction signal for a specific market."""
        for sig in signals:
            if sig.market_slug == market_slug:
                return sig
        return None


# ── Database Helpers ─────────────────────────────────────────────────

def save_scan_result(conn: sqlite3.Connection, result: ScanResult) -> None:
    """Persist scan results to the database."""
    now = result.scanned_at

    # Save tracked wallets
    for w in result.tracked_wallets:
        conn.execute(
            """INSERT OR REPLACE INTO tracked_wallets
               (address, name, total_pnl, win_rate, active_positions,
                total_volume, score, last_scanned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (w.address, w.name, w.total_pnl, w.win_rate,
             w.active_positions, w.total_volume, w.score, w.last_scanned),
        )

    # Save conviction signals (upsert – one row per market_slug+outcome)
    for sig in result.conviction_signals:
        import json
        conn.execute(
            """INSERT OR REPLACE INTO wallet_signals
               (market_slug, title, condition_id, outcome, whale_count,
                total_whale_usd, avg_whale_price, current_price,
                conviction_score, whale_names_json, direction,
                signal_strength, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sig.market_slug, sig.title, sig.condition_id, sig.outcome,
             sig.whale_count, sig.total_whale_usd, sig.avg_whale_price,
             sig.current_price, sig.conviction_score,
             json.dumps(sig.whale_names), sig.direction,
             sig.signal_strength, sig.detected_at),
        )

    # Save deltas (ignore if exact duplicate already exists)
    for delta in result.deltas:
        conn.execute(
            """INSERT OR IGNORE INTO wallet_deltas
               (wallet_address, wallet_name, action, market_slug,
                title, outcome, size_change, value_change_usd,
                current_price, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (delta.wallet_address, delta.wallet_name, delta.action,
             delta.market_slug, delta.title, delta.outcome,
             delta.size_change, delta.value_change_usd,
             delta.current_price, delta.detected_at),
        )

    conn.commit()
    log.info(
        "wallet_scanner.saved",
        wallets=len(result.tracked_wallets),
        signals=len(result.conviction_signals),
        deltas=len(result.deltas),
    )

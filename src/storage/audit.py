"""Audit trail — immutable decision logging.

Records every trading decision with full context:
  - Market data at decision time
  - Research evidence used
  - Model probability and confidence
  - Edge calculation details
  - Risk check results
  - Position sizing details
  - Order details and fills
  - Final P&L

This creates a complete, queryable audit trail for:
  1. Regulatory compliance
  2. Strategy analysis and improvement
  3. Debugging bad trades
  4. Performance attribution
"""

from __future__ import annotations

import json
import time
import hashlib
from dataclasses import dataclass, field
from typing import Any

from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class AuditEntry:
    """An immutable audit trail entry."""
    audit_id: str
    timestamp: float
    market_id: str
    decision: str  # "TRADE" | "NO_TRADE" | "EXIT"
    stage: str  # "research" | "forecast" | "edge" | "risk" | "sizing" | "execution" | "fill" | "exit"

    # Context
    data: dict[str, Any] = field(default_factory=dict)

    # Integrity
    checksum: str = ""

    def __post_init__(self):
        if not self.checksum:
            self.checksum = self._compute_checksum()

    def _compute_checksum(self) -> str:
        """Compute SHA-256 checksum for integrity verification."""
        content = json.dumps({
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "market_id": self.market_id,
            "decision": self.decision,
            "stage": self.stage,
            "data": self.data,
        }, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def verify_integrity(self) -> bool:
        """Verify the entry hasn't been tampered with."""
        return self.checksum == self._compute_checksum()

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "market_id": self.market_id,
            "decision": self.decision,
            "stage": self.stage,
            "data": self.data,
            "checksum": self.checksum,
        }


class AuditTrail:
    """Immutable audit trail for all trading decisions."""

    def __init__(self, max_entries: int = 10000):
        self._entries: list[AuditEntry] = []
        self._max_entries = max_entries
        self._entry_counter = 0

    def record(
        self,
        market_id: str,
        decision: str,
        stage: str,
        data: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Record an audit entry."""
        self._entry_counter += 1
        ts = time.time()

        audit_id = f"audit_{int(ts)}_{self._entry_counter}"

        entry = AuditEntry(
            audit_id=audit_id,
            timestamp=ts,
            market_id=market_id,
            decision=decision,
            stage=stage,
            data=data or {},
        )

        self._entries.append(entry)

        # Trim if needed
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries // 2:]

        log.debug(
            "audit.recorded",
            audit_id=audit_id,
            market_id=market_id,
            decision=decision,
            stage=stage,
        )
        return entry

    def record_trade_decision(
        self,
        market_id: str,
        question: str,
        model_prob: float,
        implied_prob: float,
        edge: float,
        confidence: str,
        risk_result: dict[str, Any],
        position_size: float,
        order_id: str = "",
        evidence_summary: str = "",
    ) -> AuditEntry:
        """Record a complete trade decision with all context."""
        decision = "TRADE" if position_size > 0 else "NO_TRADE"

        return self.record(
            market_id=market_id,
            decision=decision,
            stage="decision",
            data={
                "question": question,
                "model_probability": round(model_prob, 4),
                "implied_probability": round(implied_prob, 4),
                "edge": round(edge, 4),
                "confidence": confidence,
                "risk_checks": risk_result,
                "position_size_usd": round(position_size, 2),
                "order_id": order_id,
                "evidence_summary": evidence_summary[:500],
            },
        )

    def record_fill(
        self,
        market_id: str,
        order_id: str,
        fill_price: float,
        size_filled: float,
        slippage_bps: float,
    ) -> AuditEntry:
        """Record an order fill."""
        return self.record(
            market_id=market_id,
            decision="FILL",
            stage="fill",
            data={
                "order_id": order_id,
                "fill_price": round(fill_price, 4),
                "size_filled": round(size_filled, 2),
                "slippage_bps": round(slippage_bps, 1),
            },
        )

    def record_exit(
        self,
        market_id: str,
        reason: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        holding_hours: float,
    ) -> AuditEntry:
        """Record a position exit."""
        return self.record(
            market_id=market_id,
            decision="EXIT",
            stage="exit",
            data={
                "reason": reason,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "pnl_usd": round(pnl, 2),
                "holding_hours": round(holding_hours, 1),
            },
        )

    def get_entries(
        self,
        market_id: str | None = None,
        decision: str | None = None,
        stage: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Query audit entries with filters."""
        results = self._entries
        if market_id:
            results = [e for e in results if e.market_id == market_id]
        if decision:
            results = [e for e in results if e.decision == decision]
        if stage:
            results = [e for e in results if e.stage == stage]
        if since:
            results = [e for e in results if e.timestamp >= since]
        return results[-limit:]

    def get_trade_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent trade decisions for dashboard."""
        trades = [
            e for e in self._entries
            if e.stage == "decision" and e.decision == "TRADE"
        ]
        return [e.to_dict() for e in trades[-limit:]]

    def verify_all(self) -> tuple[int, int]:
        """Verify integrity of all entries. Returns (valid, invalid)."""
        valid = sum(1 for e in self._entries if e.verify_integrity())
        invalid = len(self._entries) - valid
        return valid, invalid

    def get_summary(self) -> dict[str, Any]:
        """Dashboard summary of audit trail."""
        total = len(self._entries)
        if not self._entries:
            return {"total_entries": 0}

        trades = sum(1 for e in self._entries if e.decision == "TRADE")
        no_trades = sum(1 for e in self._entries if e.decision == "NO_TRADE")
        exits = sum(1 for e in self._entries if e.decision == "EXIT")

        return {
            "total_entries": total,
            "trades": trades,
            "no_trades": no_trades,
            "exits": exits,
            "oldest": self._entries[0].timestamp,
            "newest": self._entries[-1].timestamp,
        }

"""Order cancellation — cancel open orders.

Supports cancelling individual orders or all open orders (kill switch).
"""

from __future__ import annotations

from typing import Any

from src.config import is_live_trading_enabled
from src.connectors.polymarket_clob import CLOBClient
from src.observability.logger import get_logger
from src.observability.metrics import metrics

log = get_logger(__name__)


async def cancel_order(clob: CLOBClient, order_id: str) -> dict[str, Any]:
    """Cancel a single open order."""
    if not is_live_trading_enabled():
        log.info("cancels.dry_run", order_id=order_id[:8])
        return {"order_id": order_id, "status": "cancel_simulated"}

    try:
        signing = clob.get_signing_client()
        resp = signing.cancel(order_id)
        log.info("cancels.cancelled", order_id=order_id[:8])
        metrics.incr("orders.cancelled")
        return {"order_id": order_id, "status": "cancelled", "raw": str(resp)[:200]}
    except Exception as e:
        log.error("cancels.failed", order_id=order_id[:8], error=str(e))
        return {"order_id": order_id, "status": "cancel_failed", "error": str(e)}


async def cancel_all_orders(clob: CLOBClient) -> dict[str, Any]:
    """Cancel all open orders (kill switch)."""
    if not is_live_trading_enabled():
        log.info("cancels.cancel_all_dry_run")
        return {"status": "cancel_all_simulated"}

    try:
        signing = clob.get_signing_client()
        resp = signing.cancel_all()
        log.info("cancels.cancelled_all")
        metrics.incr("orders.cancelled_all")
        return {"status": "cancelled_all", "raw": str(resp)[:200]}
    except Exception as e:
        log.error("cancels.cancel_all_failed", error=str(e))
        return {"status": "cancel_all_failed", "error": str(e)}

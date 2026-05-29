"""Polymarket Data API connector.

The Data API provides user-level position, activity, and trade data.
We use it to track whale wallets and extract smart-money signals.

Base URL: https://data-api.polymarket.com
Endpoints:
  - GET /positions?user={address}&sortBy=CASHPNL&limit=100
  - GET /activity?user={address}&limit=100
  - GET /trades?user={address}&limit=100
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.observability.logger import get_logger

log = get_logger(__name__)

DATA_API_BASE = "https://data-api.polymarket.com"

# Default timeout & headers
_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "polymarket-bot/1.0",
}


# ── Data Models ──────────────────────────────────────────────────────

@dataclass
class WalletPosition:
    """A single position held by a wallet on Polymarket."""
    proxy_wallet: str = ""
    asset: str = ""           # token_id
    condition_id: str = ""
    market_slug: str = ""
    title: str = ""
    outcome: str = ""         # "Yes" / "No"
    size: float = 0.0         # number of shares
    avg_price: float = 0.0
    cur_price: float = 0.0
    initial_value: float = 0.0
    current_value: float = 0.0
    cash_pnl: float = 0.0
    percent_pnl: float = 0.0
    end_date: str = ""
    realized: bool = False

    @property
    def is_profitable(self) -> bool:
        return self.cash_pnl > 0

    @property
    def unrealised_return_pct(self) -> float:
        if self.initial_value == 0:
            return 0.0
        return (self.current_value - self.initial_value) / self.initial_value * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "proxy_wallet": self.proxy_wallet,
            "asset": self.asset,
            "condition_id": self.condition_id,
            "market_slug": self.market_slug,
            "title": self.title,
            "outcome": self.outcome,
            "size": round(self.size, 4),
            "avg_price": round(self.avg_price, 4),
            "cur_price": round(self.cur_price, 4),
            "initial_value": round(self.initial_value, 2),
            "current_value": round(self.current_value, 2),
            "cash_pnl": round(self.cash_pnl, 2),
            "percent_pnl": round(self.percent_pnl, 2),
            "end_date": self.end_date,
            "realized": self.realized,
        }


@dataclass
class WalletActivity:
    """A single activity event for a wallet (buy/sell/redeem)."""
    transaction_hash: str = ""
    action: str = ""          # "Buy", "Sell", "Redeem"
    market_slug: str = ""
    title: str = ""
    outcome: str = ""
    size: float = 0.0
    price: float = 0.0
    value_usd: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_hash": self.transaction_hash,
            "action": self.action,
            "market_slug": self.market_slug,
            "title": self.title,
            "outcome": self.outcome,
            "size": round(self.size, 4),
            "price": round(self.price, 4),
            "value_usd": round(self.value_usd, 2),
            "timestamp": self.timestamp,
        }


# ── Client ───────────────────────────────────────────────────────────

class DataAPIClient:
    """Async client for Polymarket's Data API (user positions & activity)."""

    def __init__(self, base_url: str = DATA_API_BASE):
        self._base = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                timeout=_TIMEOUT,
                headers=_HEADERS,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Positions ────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def get_positions(
        self,
        address: str,
        *,
        sort_by: str = "CURRENT",
        limit: int = 100,
        offset: int = 0,
    ) -> list[WalletPosition]:
        """Fetch positions for a wallet address.

        sort_by: CURRENT | INITIAL | TOKENS | CASHPNL | PERCENTPNL
        """
        client = await self._ensure_client()
        params: dict[str, Any] = {
            "user": address.lower(),
            "sortBy": sort_by,
            "limit": limit,
            "offset": offset,
        }
        resp = await client.get("/positions", params=params)
        resp.raise_for_status()
        data = resp.json()

        positions: list[WalletPosition] = []
        # API returns a list of position objects
        items = data if isinstance(data, list) else data.get("positions", data.get("data", []))
        for item in items:
            positions.append(_parse_position(item))

        log.debug(
            "data_api.positions_fetched",
            address=address[:10],
            count=len(positions),
        )
        return positions

    # ── Activity (recent buys/sells) ─────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def get_activity(
        self,
        address: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WalletActivity]:
        """Fetch recent activity (trades) for a wallet."""
        client = await self._ensure_client()
        params: dict[str, Any] = {
            "user": address.lower(),
            "limit": limit,
            "offset": offset,
        }
        resp = await client.get("/activity", params=params)
        resp.raise_for_status()
        data = resp.json()

        activities: list[WalletActivity] = []
        items = data if isinstance(data, list) else data.get("activity", data.get("data", []))
        for item in items:
            activities.append(_parse_activity(item))

        log.debug(
            "data_api.activity_fetched",
            address=address[:10],
            count=len(activities),
        )
        return activities


# ── Parsers ──────────────────────────────────────────────────────────

def _parse_position(raw: dict[str, Any]) -> WalletPosition:
    """Parse a raw position object from the Data API."""
    return WalletPosition(
        proxy_wallet=str(raw.get("proxyWallet", raw.get("proxy_wallet", ""))),
        asset=str(raw.get("asset", "")),
        condition_id=str(raw.get("conditionId", raw.get("condition_id", ""))),
        market_slug=str(raw.get("slug", raw.get("market_slug", ""))),
        title=str(raw.get("title", "")),
        outcome=str(raw.get("outcome", "")),
        size=float(raw.get("size", 0)),
        avg_price=float(raw.get("avgPrice", raw.get("avg_price", 0))),
        cur_price=float(raw.get("curPrice", raw.get("cur_price", 0))),
        initial_value=float(raw.get("initialValue", raw.get("initial_value", 0))),
        current_value=float(raw.get("currentValue", raw.get("current_value", 0))),
        cash_pnl=float(raw.get("cashPnl", raw.get("cash_pnl", 0))),
        percent_pnl=float(raw.get("percentPnl", raw.get("percent_pnl", 0))),
        end_date=str(raw.get("endDate", raw.get("end_date", ""))),
        realized=bool(raw.get("realized", False)),
    )


def _parse_activity(raw: dict[str, Any]) -> WalletActivity:
    """Parse a raw activity object from the Data API."""
    size = float(raw.get("size", raw.get("amount", 0)))
    price = float(raw.get("price", 0))
    value = float(raw.get("value", raw.get("usdcSize", 0)))
    if value == 0 and size > 0 and price > 0:
        value = size * price

    return WalletActivity(
        transaction_hash=str(raw.get("transactionHash", raw.get("transaction_hash", ""))),
        action=str(raw.get("type", raw.get("action", ""))),
        market_slug=str(raw.get("slug", raw.get("market_slug", ""))),
        title=str(raw.get("title", "")),
        outcome=str(raw.get("outcome", "")),
        size=size,
        price=price,
        value_usd=value,
        timestamp=str(raw.get("timestamp", raw.get("createdAt", ""))),
    )

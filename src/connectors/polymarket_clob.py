"""Polymarket CLOB (Central-Limit Order Book) connector.

Handles:
  - Fetching real-time orderbooks (bids/asks)
  - Price history / trade history
  - Order placement (via py-clob-client when available)
  - Spread & liquidity calculations
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.observability.logger import get_logger

log = get_logger(__name__)

CLOB_BASE = "https://clob.polymarket.com"


# ── Data Models ──────────────────────────────────────────────────────

@dataclass
class OrderBookLevel:
    price: float
    size: float

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class OrderBook:
    """Snapshot of an order book for one token."""
    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        return self.spread / mid if mid > 0 else float("inf")

    def bid_depth(self, levels: int = 5) -> float:
        """Total notional on bid side up to N levels."""
        return sum(b.notional for b in self.bids[:levels])

    def ask_depth(self, levels: int = 5) -> float:
        """Total notional on ask side up to N levels."""
        return sum(a.notional for a in self.asks[:levels])


@dataclass
class TradeRecord:
    price: float
    size: float
    side: str  # "buy" | "sell"
    timestamp: float = 0.0


# ── Client ───────────────────────────────────────────────────────────

class CLOBClient:
    """Async client for the Polymarket CLOB REST API."""

    def __init__(
        self,
        base_url: str = CLOB_BASE,
        timeout: float = 30.0,
    ):
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self._signing_client: Any = None

    async def close(self) -> None:
        await self._client.aclose()

    def _ensure_signing_client(self) -> Any:
        """Lazy-load the py-clob-client for authenticated operations."""
        if self._signing_client is not None:
            return self._signing_client

        try:
            from py_clob_client.client import ClobClient  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                "py-clob-client is required for authenticated CLOB operations. "
                "Install it with: pip install py-clob-client"
            )

        api_key = os.environ.get("POLYMARKET_API_KEY", "")
        api_secret = os.environ.get("POLYMARKET_API_SECRET", "")
        api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "")
        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))

        if not all([api_key, api_secret, api_passphrase, private_key]):
            raise RuntimeError(
                "CLOB signing requires POLYMARKET_API_KEY, POLYMARKET_API_SECRET, "
                "POLYMARKET_API_PASSPHRASE, and POLYMARKET_PRIVATE_KEY env vars."
            )

        # SECURITY: Never log the private key
        log.info("clob.init_signing_client", chain_id=chain_id, api_key=api_key[:8] + "***")

        self._signing_client = ClobClient(
            host=self._base,
            key=private_key,
            chain_id=chain_id,
            creds={
                "apiKey": api_key,
                "secret": api_secret,
                "passphrase": api_passphrase,
            },
        )
        return self._signing_client

    # ── Public read endpoints ────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_orderbook(self, token_id: str) -> OrderBook:
        """Fetch the current order book for a token."""
        data = await self._get("/book", params={"token_id": token_id})
        return parse_orderbook(token_id, data)

    async def get_price(self, token_id: str) -> float:
        """Fetch the last traded / mid price for a token."""
        data = await self._get("/price", params={"token_id": token_id})
        return float(data.get("price", data.get("mid", 0)))

    async def get_trade_history(
        self, token_id: str, limit: int = 100
    ) -> list[TradeRecord]:
        """Fetch recent trades for a token."""
        data = await self._get(
            "/trades",
            params={"token_id": token_id, "limit": limit},
        )
        trades: list[TradeRecord] = []
        items = data if isinstance(data, list) else data.get("data", [])
        for t in items:
            trades.append(
                TradeRecord(
                    price=float(t.get("price", 0)),
                    size=float(t.get("size", t.get("amount", 0))),
                    side=t.get("side", "unknown"),
                    timestamp=float(t.get("timestamp", t.get("time", 0))),
                )
            )
        return trades

    def get_signing_client(self) -> Any:
        """Return the py-clob-client instance for order operations."""
        return self._ensure_signing_client()


# ── Parsing helpers ──────────────────────────────────────────────────

def parse_orderbook(token_id: str, data: dict[str, Any]) -> OrderBook:
    """Parse raw CLOB orderbook JSON into an OrderBook."""
    bids: list[OrderBookLevel] = []
    asks: list[OrderBookLevel] = []

    for b in data.get("bids", []):
        bids.append(OrderBookLevel(
            price=float(b.get("price", 0)),
            size=float(b.get("size", 0)),
        ))
    for a in data.get("asks", []):
        asks.append(OrderBookLevel(
            price=float(a.get("price", 0)),
            size=float(a.get("size", 0)),
        ))

    # Sort: bids descending, asks ascending
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)

    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        timestamp=float(data.get("timestamp", 0)),
    )

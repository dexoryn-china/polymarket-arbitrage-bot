"""Websocket feed — real-time price streaming from Polymarket CLOB.

Maintains persistent websocket connections for:
  - Orderbook updates (L2 incremental)
  - Trade prints (live fills)
  - Price ticker (bid/ask/mid updates)

Supports auto-reconnection with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from src.observability.logger import get_logger

log = get_logger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class PriceTick:
    """A single price update."""
    token_id: str
    best_bid: float
    best_ask: float
    mid: float
    timestamp: float
    volume_24h: float = 0.0


@dataclass
class LiveTrade:
    """A live trade from the websocket feed."""
    token_id: str
    price: float
    size: float
    side: str
    timestamp: float


TickCallback = Callable[[PriceTick], Coroutine[Any, Any, None]]
TradeCallback = Callable[[LiveTrade], Coroutine[Any, Any, None]]


class WebSocketFeed:
    """Manage websocket connections to Polymarket CLOB."""

    def __init__(self, url: str = WS_URL):
        self._url = url
        self._subscribed_tokens: set[str] = set()
        self._tick_callbacks: list[TickCallback] = []
        self._trade_callbacks: list[TradeCallback] = []
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._last_prices: dict[str, PriceTick] = {}
        self._ws: Any = None

    def on_tick(self, callback: TickCallback) -> None:
        """Register a callback for price tick updates."""
        self._tick_callbacks.append(callback)

    def on_trade(self, callback: TradeCallback) -> None:
        """Register a callback for live trades."""
        self._trade_callbacks.append(callback)

    def subscribe(self, token_id: str) -> None:
        """Subscribe to updates for a token."""
        self._subscribed_tokens.add(token_id)

    def unsubscribe(self, token_id: str) -> None:
        """Unsubscribe from a token."""
        self._subscribed_tokens.discard(token_id)

    def get_last_price(self, token_id: str) -> PriceTick | None:
        """Get the last known price for a token."""
        return self._last_prices.get(token_id)

    async def start(self) -> None:
        """Start the websocket feed with auto-reconnection."""
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                if not self._running:
                    break
                log.warning(
                    "ws_feed.disconnected",
                    error=str(e),
                    reconnect_delay=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._max_reconnect_delay,
                )

    async def stop(self) -> None:
        """Stop the websocket feed."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _connect(self) -> None:
        """Establish websocket connection and process messages."""
        try:
            import websockets
        except ImportError:
            log.error("ws_feed.missing_dep", msg="websockets package required")
            return

        log.info("ws_feed.connecting", url=self._url)
        async with websockets.connect(self._url) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0  # Reset on successful connect
            log.info("ws_feed.connected", tokens=len(self._subscribed_tokens))

            # Send subscriptions
            for token_id in self._subscribed_tokens:
                sub_msg = json.dumps({
                    "type": "subscribe",
                    "channel": "market",
                    "assets_ids": [token_id],
                })
                await ws.send(sub_msg)

            # Process incoming messages
            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg) if isinstance(raw_msg, str) else json.loads(raw_msg.decode())
                    await self._handle_message(msg)
                except Exception as e:
                    log.debug("ws_feed.parse_error", error=str(e))

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        """Route incoming websocket messages to appropriate handlers."""
        msg_type = msg.get("type", msg.get("event_type", ""))

        if msg_type in ("book", "price_change", "tick"):
            await self._handle_tick(msg)
        elif msg_type in ("trade", "last_trade_price"):
            await self._handle_trade(msg)

    async def _handle_tick(self, msg: dict[str, Any]) -> None:
        """Process a price tick message."""
        token_id = msg.get("asset_id", msg.get("token_id", ""))
        if not token_id:
            return

        tick = PriceTick(
            token_id=token_id,
            best_bid=float(msg.get("best_bid", msg.get("bid", 0))),
            best_ask=float(msg.get("best_ask", msg.get("ask", 0))),
            mid=float(msg.get("mid", 0)),
            timestamp=float(msg.get("timestamp", time.time())),
        )
        if tick.mid == 0 and tick.best_bid > 0 and tick.best_ask > 0:
            tick.mid = (tick.best_bid + tick.best_ask) / 2

        self._last_prices[token_id] = tick

        for cb in self._tick_callbacks:
            try:
                await cb(tick)
            except Exception as e:
                log.error("ws_feed.tick_callback_error", error=str(e))

    async def _handle_trade(self, msg: dict[str, Any]) -> None:
        """Process a trade message."""
        token_id = msg.get("asset_id", msg.get("token_id", ""))
        if not token_id:
            return

        trade = LiveTrade(
            token_id=token_id,
            price=float(msg.get("price", 0)),
            size=float(msg.get("size", msg.get("amount", 0))),
            side=msg.get("side", "unknown"),
            timestamp=float(msg.get("timestamp", time.time())),
        )

        for cb in self._trade_callbacks:
            try:
                await cb(trade)
            except Exception as e:
                log.error("ws_feed.trade_callback_error", error=str(e))

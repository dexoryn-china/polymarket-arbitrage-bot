"""Tests for CLOB orderbook parsing and calculations."""

from __future__ import annotations

from src.connectors.polymarket_clob import (
    OrderBook,
    OrderBookLevel,
    parse_orderbook,
)


class TestOrderBookLevel:
    def test_notional(self) -> None:
        level = OrderBookLevel(price=0.65, size=100.0)
        assert abs(level.notional - 65.0) < 0.01


class TestOrderBook:
    def _make_book(self) -> OrderBook:
        return OrderBook(
            token_id="tok_abc",
            bids=[
                OrderBookLevel(price=0.64, size=200),
                OrderBookLevel(price=0.63, size=150),
                OrderBookLevel(price=0.62, size=300),
            ],
            asks=[
                OrderBookLevel(price=0.66, size=180),
                OrderBookLevel(price=0.67, size=250),
                OrderBookLevel(price=0.68, size=100),
            ],
        )

    def test_best_bid(self) -> None:
        book = self._make_book()
        assert book.best_bid == 0.64

    def test_best_ask(self) -> None:
        book = self._make_book()
        assert book.best_ask == 0.66

    def test_mid(self) -> None:
        book = self._make_book()
        assert abs(book.mid - 0.65) < 0.001

    def test_spread(self) -> None:
        book = self._make_book()
        assert abs(book.spread - 0.02) < 0.001

    def test_spread_pct(self) -> None:
        book = self._make_book()
        expected = 0.02 / 0.65
        assert abs(book.spread_pct - expected) < 0.001

    def test_bid_depth(self) -> None:
        book = self._make_book()
        # 0.64*200 + 0.63*150 + 0.62*300 = 128 + 94.5 + 186 = 408.5
        assert abs(book.bid_depth(3) - 408.5) < 0.1

    def test_ask_depth(self) -> None:
        book = self._make_book()
        # 0.66*180 + 0.67*250 + 0.68*100 = 118.8 + 167.5 + 68 = 354.3
        assert abs(book.ask_depth(3) - 354.3) < 0.1

    def test_empty_book(self) -> None:
        book = OrderBook(token_id="empty")
        assert book.best_bid == 0.0
        assert book.best_ask == 1.0
        assert book.mid == 0.5
        assert book.spread == 1.0
        assert book.bid_depth(5) == 0.0
        assert book.ask_depth(5) == 0.0

    def test_single_level(self) -> None:
        book = OrderBook(
            token_id="single",
            bids=[OrderBookLevel(price=0.70, size=500)],
            asks=[OrderBookLevel(price=0.72, size=500)],
        )
        assert book.best_bid == 0.70
        assert book.best_ask == 0.72
        assert abs(book.spread - 0.02) < 0.001


class TestParseOrderbook:
    def test_parse_basic(self) -> None:
        data = {
            "bids": [
                {"price": "0.65", "size": "100"},
                {"price": "0.64", "size": "200"},
            ],
            "asks": [
                {"price": "0.67", "size": "150"},
                {"price": "0.66", "size": "50"},
            ],
            "timestamp": 1700000000,
        }
        book = parse_orderbook("tok_test", data)
        assert book.token_id == "tok_test"
        # Bids sorted descending
        assert book.bids[0].price == 0.65
        assert book.bids[1].price == 0.64
        # Asks sorted ascending
        assert book.asks[0].price == 0.66
        assert book.asks[1].price == 0.67
        assert book.timestamp == 1700000000

    def test_parse_empty(self) -> None:
        data: dict = {"bids": [], "asks": []}
        book = parse_orderbook("tok_empty", data)
        assert len(book.bids) == 0
        assert len(book.asks) == 0

    def test_parse_numeric_strings(self) -> None:
        """Ensure string prices/sizes are converted to floats."""
        data = {
            "bids": [{"price": "0.50", "size": "1000"}],
            "asks": [{"price": "0.55", "size": "500"}],
        }
        book = parse_orderbook("tok_str", data)
        assert isinstance(book.bids[0].price, float)
        assert isinstance(book.bids[0].size, float)
        assert book.bids[0].price == 0.50
        assert book.bids[0].size == 1000.0

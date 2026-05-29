"""Feature builder — construct features for the forecasting model.

Combines:
  - Market metadata (volume, liquidity, time to expiry, type)
  - Orderbook features (spread, depth, imbalance)
  - Evidence features (quality, num sources, contradictions)
  - Price history features (momentum, volatility)
  - Microstructure signals (VWAP, flow imbalance, whale activity)
  - Timeline features (resolution urgency, time decay)

IMPORTANT: Implied probability uses mid-price (best_bid + best_ask) / 2,
not just the bid, to avoid systematic bias.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from src.connectors.polymarket_clob import OrderBook, TradeRecord
from src.connectors.polymarket_gamma import GammaMarket
from src.research.evidence_extractor import EvidencePackage
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class MarketFeatures:
    """Feature vector for forecasting."""
    market_id: str = ""
    question: str = ""
    market_type: str = "UNKNOWN"

    # Market metadata
    volume_usd: float = 0.0
    liquidity_usd: float = 0.0
    days_to_expiry: float = 0.0
    category: str = ""
    has_clear_resolution: bool = False

    # Price / orderbook
    implied_probability: float = 0.5
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    spread_pct: float = 0.0
    bid_depth_5: float = 0.0
    ask_depth_5: float = 0.0
    book_imbalance: float = 0.0  # (bid - ask) / (bid + ask)

    # Price history
    price_24h_ago: float = 0.0
    price_momentum: float = 0.0
    price_volatility: float = 0.0
    num_recent_trades: int = 0

    # Evidence
    evidence_quality: float = 0.0
    num_sources: int = 0
    num_contradictions: int = 0
    has_numeric_evidence: bool = False
    num_evidence_bullets: int = 0

    # Microstructure signals
    vwap: float = 0.0
    vwap_divergence_pct: float = 0.0
    flow_imbalance_1h: float = 0.0
    flow_imbalance_4h: float = 0.0
    flow_imbalance_24h: float = 0.0
    whale_net_flow: float = 0.0
    whale_count: int = 0
    trade_acceleration: float = 1.0
    depth_ratio: float = 1.0
    smart_money_direction: str = "neutral"

    # Timeline features
    hours_to_resolution: float = 0.0
    is_near_resolution: bool = False
    resolution_urgency: float = 0.0  # 0-1, higher = closer to resolution
    time_decay_multiplier: float = 1.0

    # Derived
    evidence_summary: str = ""
    top_bullets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


def build_features(
    market: GammaMarket,
    orderbook: OrderBook | None = None,
    trades: list[TradeRecord] | None = None,
    evidence: EvidencePackage | None = None,
    microstructure: Any | None = None,
) -> MarketFeatures:
    """Construct a feature vector from all available data.

    Uses mid-price (not just bid) for implied probability.
    """
    features = MarketFeatures(
        market_id=market.id,
        question=market.question,
        market_type=market.market_type,
        volume_usd=market.volume,
        liquidity_usd=market.liquidity,
        category=market.category,
        has_clear_resolution=market.has_clear_resolution,
    )

    # Initial implied probability from token prices (use mid if available)
    yes_tokens = [t for t in market.tokens if t.outcome.lower() == "yes"]
    no_tokens = [t for t in market.tokens if t.outcome.lower() == "no"]
    if yes_tokens and no_tokens:
        yes_price = yes_tokens[0].price
        no_price = no_tokens[0].price
        # Mid-price adjusted for vig
        total = yes_price + no_price
        if total > 0:
            features.implied_probability = yes_price / total
        else:
            features.implied_probability = yes_price
    elif yes_tokens:
        features.implied_probability = yes_tokens[0].price
    elif len(market.tokens) >= 2:
        # Non-Yes/No market — use first token as the "positive" outcome
        first_price = market.tokens[0].price
        second_price = market.tokens[1].price
        total = first_price + second_price
        if total > 0:
            features.implied_probability = first_price / total
        else:
            features.implied_probability = first_price
    elif market.tokens:
        features.implied_probability = market.tokens[0].price
    else:
        features.implied_probability = market.best_bid

    # Days to expiry + timeline features
    if market.end_date:
        now = dt.datetime.now(dt.timezone.utc)
        delta = market.end_date - now
        total_secs = max(delta.total_seconds(), 0)
        features.days_to_expiry = total_secs / 86400
        features.hours_to_resolution = total_secs / 3600

        # Resolution urgency (0-1, increases as resolution approaches)
        if features.days_to_expiry <= 2:
            features.is_near_resolution = True
            features.resolution_urgency = min(1.0, 1.0 - (features.days_to_expiry / 2))
        elif features.days_to_expiry <= 7:
            features.resolution_urgency = 0.3 + 0.5 * (1.0 - features.days_to_expiry / 7)
        else:
            features.resolution_urgency = max(0.0, 0.3 - features.days_to_expiry / 100)

        # Time decay multiplier for position sizing
        if features.days_to_expiry <= 7:
            features.time_decay_multiplier = min(1.5, 1.0 + (7 - features.days_to_expiry) / 14)
        else:
            features.time_decay_multiplier = 1.0

    # Orderbook features — use mid-price for implied probability
    if orderbook:
        features.spread = orderbook.spread
        features.spread_pct = orderbook.spread_pct
        features.best_bid = orderbook.best_bid
        features.best_ask = orderbook.best_ask
        # CRITICAL FIX: Use mid-price, not best_bid, for implied probability
        features.implied_probability = orderbook.mid
        features.bid_depth_5 = orderbook.bid_depth(5)
        features.ask_depth_5 = orderbook.ask_depth(5)
        total = features.bid_depth_5 + features.ask_depth_5
        if total > 0:
            features.book_imbalance = (features.bid_depth_5 - features.ask_depth_5) / total

    # Trade history features
    if trades:
        features.num_recent_trades = len(trades)
        prices = [t.price for t in trades if t.price > 0]
        if prices:
            features.price_24h_ago = prices[-1] if len(prices) > 1 else prices[0]
            features.price_momentum = prices[0] - features.price_24h_ago
            if len(prices) >= 2:
                mean_p = sum(prices) / len(prices)
                variance = sum((p - mean_p) ** 2 for p in prices) / len(prices)
                features.price_volatility = variance ** 0.5

    # Evidence features
    if evidence:
        features.evidence_quality = evidence.quality_score
        features.num_sources = evidence.num_sources
        features.num_contradictions = len(evidence.contradictions)
        features.num_evidence_bullets = len(evidence.bullets)
        features.has_numeric_evidence = any(b.is_numeric for b in evidence.bullets)
        features.evidence_summary = evidence.summary
        features.top_bullets = [
            f"{b.text} [{b.citation.publisher}, {b.citation.date}]"
            for b in sorted(evidence.bullets, key=lambda x: x.relevance, reverse=True)[:5]
        ]

    # Microstructure signals
    if microstructure is not None:
        features.vwap = microstructure.vwap
        features.vwap_divergence_pct = microstructure.vwap_divergence_pct
        features.whale_net_flow = microstructure.whale_net_flow
        features.whale_count = len(microstructure.whale_alerts)
        features.trade_acceleration = microstructure.trade_acceleration
        features.depth_ratio = microstructure.depth_ratio
        features.smart_money_direction = microstructure.large_trade_direction

        # Extract flow imbalance by window
        for fi in microstructure.flow_imbalances:
            if fi.window_minutes == 60:
                features.flow_imbalance_1h = fi.imbalance_ratio
            elif fi.window_minutes == 240:
                features.flow_imbalance_4h = fi.imbalance_ratio
            elif fi.window_minutes == 1440:
                features.flow_imbalance_24h = fi.imbalance_ratio

    log.info(
        "feature_builder.built",
        market_id=features.market_id,
        implied_prob=round(features.implied_probability, 3),
        evidence_quality=round(features.evidence_quality, 2),
        days_to_expiry=round(features.days_to_expiry, 1),
        urgency=round(features.resolution_urgency, 2),
    )
    return features

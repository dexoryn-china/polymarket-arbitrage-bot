"""Linked-market / arbitrage detection.

Identifies markets that reference the same event and finds
pricing inconsistencies that can be exploited. Examples:

  - "Will X happen before Y?" and "Will Y happen before X?"
    should sum to ~1.0 after vig
  - Multi-outcome markets where all options should sum to ~1.0
  - Correlated markets with divergent pricing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.connectors.polymarket_gamma import GammaMarket
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class ArbitrageOpportunity:
    """An identified pricing inconsistency."""
    market_ids: list[str]
    questions: list[str]
    implied_probs: list[float]
    prob_sum: float  # should be ~1.0 for complementary markets
    arb_edge: float  # how much the sum deviates from 1.0
    arb_type: str  # "complementary", "multi_outcome", "correlated"
    description: str
    is_actionable: bool  # True if edge > transaction costs

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


def detect_arbitrage(
    markets: list[GammaMarket],
    fee_bps: int = 200,
) -> list[ArbitrageOpportunity]:
    """Scan a list of markets for arbitrage opportunities.

    Strategies:
    1. Complementary binary markets (same event, opposite outcomes)
    2. Multi-outcome markets (all outcomes should sum to ~1.0)
    3. Similar-question markets with different pricing
    """
    opportunities: list[ArbitrageOpportunity] = []

    # Strategy 1: Group by event_slug and find complementary pairs
    event_groups: dict[str, list[GammaMarket]] = {}
    for m in markets:
        slug = m.slug.rsplit("-", 1)[0] if m.slug else m.id
        event_groups.setdefault(slug, []).append(m)

    for slug, group in event_groups.items():
        if len(group) < 2:
            continue

        # Check if probabilities sum correctly
        probs = []
        for m in group:
            yes_tokens = [t for t in m.tokens if t.outcome.lower() == "yes"]
            if yes_tokens:
                probs.append(yes_tokens[0].price)
            else:
                probs.append(m.best_bid)

        prob_sum = sum(probs)
        fee_cost = fee_bps / 10000 * len(group)

        # For complementary markets, sum should be ~1.0
        # If sum differs significantly, there's an opportunity
        deviation = abs(prob_sum - 1.0)
        edge = deviation - fee_cost

        if edge > 0.01 and len(group) >= 2:
            opportunities.append(ArbitrageOpportunity(
                market_ids=[m.id for m in group],
                questions=[m.question for m in group],
                implied_probs=probs,
                prob_sum=prob_sum,
                arb_edge=edge,
                arb_type="complementary",
                description=(
                    f"Event '{slug}': {len(group)} markets sum to "
                    f"{prob_sum:.3f} (deviation: {deviation:.3f}, "
                    f"net edge after fees: {edge:.3f})"
                ),
                is_actionable=edge > 0.02,
            ))

    # Strategy 2: Multi-outcome (markets with >2 tokens)
    for m in markets:
        if len(m.tokens) > 2:
            token_probs = [t.price for t in m.tokens]
            prob_sum = sum(token_probs)
            deviation = abs(prob_sum - 1.0)
            fee_cost = fee_bps / 10000 * 2  # buy/sell
            edge = deviation - fee_cost

            if edge > 0.01:
                opportunities.append(ArbitrageOpportunity(
                    market_ids=[m.id],
                    questions=[m.question],
                    implied_probs=token_probs,
                    prob_sum=prob_sum,
                    arb_edge=edge,
                    arb_type="multi_outcome",
                    description=(
                        f"Multi-outcome '{m.question[:60]}': "
                        f"{len(m.tokens)} outcomes sum to {prob_sum:.3f} "
                        f"(edge: {edge:.3f})"
                    ),
                    is_actionable=edge > 0.02,
                ))

    # Strategy 3: Similar questions with divergent pricing
    # Simple keyword-based similarity check
    _check_similar_questions(markets, opportunities, fee_bps)

    if opportunities:
        log.info(
            "arbitrage.detected",
            num_opportunities=len(opportunities),
            actionable=sum(1 for o in opportunities if o.is_actionable),
        )

    return sorted(opportunities, key=lambda x: x.arb_edge, reverse=True)


def _check_similar_questions(
    markets: list[GammaMarket],
    opportunities: list[ArbitrageOpportunity],
    fee_bps: int,
) -> None:
    """Find markets with similar questions but different prices."""
    # Extract key entities from questions
    market_entities: list[tuple[GammaMarket, set[str]]] = []
    for m in markets:
        words = set(m.question.lower().split())
        # Remove common words
        stop_words = {
            "will", "the", "a", "an", "be", "is", "are", "was", "were",
            "in", "on", "at", "to", "for", "of", "by", "before", "after",
            "this", "that", "or", "and", "not", "no", "yes", "?", "how",
            "what", "when", "where", "which", "who",
        }
        entities = words - stop_words
        if len(entities) >= 2:
            market_entities.append((m, entities))

    # Compare pairs (O(n^2) but typically small n)
    seen = set()
    for i, (m1, e1) in enumerate(market_entities):
        for j, (m2, e2) in enumerate(market_entities):
            if i >= j:
                continue
            pair_key = (m1.id, m2.id)
            if pair_key in seen:
                continue
            seen.add(pair_key)

            # Jaccard similarity
            intersection = e1 & e2
            union = e1 | e2
            if not union:
                continue
            similarity = len(intersection) / len(union)

            if similarity >= 0.5:
                # These markets are about similar topics — check price divergence
                p1 = m1.best_bid
                p2 = m2.best_bid
                price_diff = abs(p1 - p2)
                fee_cost = fee_bps / 10000 * 2

                if price_diff > fee_cost + 0.03:
                    opportunities.append(ArbitrageOpportunity(
                        market_ids=[m1.id, m2.id],
                        questions=[m1.question, m2.question],
                        implied_probs=[p1, p2],
                        prob_sum=p1 + p2,
                        arb_edge=price_diff - fee_cost,
                        arb_type="correlated",
                        description=(
                            f"Similar markets with {price_diff:.3f} price gap: "
                            f"'{m1.question[:40]}' ({p1:.2f}) vs "
                            f"'{m2.question[:40]}' ({p2:.2f})"
                        ),
                        is_actionable=price_diff > fee_cost + 0.05,
                    ))

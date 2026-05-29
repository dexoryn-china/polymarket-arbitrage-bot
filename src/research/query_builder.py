"""Query builder — constructs targeted web search queries from market data.

Implements the SOURCE-FIRST, WHITELISTED SEARCH PIPELINE:
  1. Site-restricted queries to primary authoritative sources
  2. Metric-specific and date-scoped queries
  3. Confirmation queries to secondary outlets
  4. Contrarian queries to surface opposing evidence
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from src.connectors.polymarket_gamma import GammaMarket
from src.observability.logger import get_logger

log = get_logger(__name__)

# ── Primary source sites by category (aligned with classifier output) ──
_SITE_RESTRICTED: dict[str, list[str]] = {
    "MACRO": [
        "site:bls.gov",
        "site:bea.gov",
        "site:federalreserve.gov",
        "site:fred.stlouisfed.org",
        "site:treasury.gov",
    ],
    "ELECTION": [
        "site:fec.gov",
        "site:ballotpedia.org",
    ],
    "CORPORATE": [
        "site:sec.gov",
    ],
    "WEATHER": [
        "site:noaa.gov",
        "site:nhc.noaa.gov",
        "site:weather.gov",
    ],
    "SCIENCE": [
        "site:nature.com",
        "site:science.org",
        "site:arxiv.org",
    ],
    "REGULATION": [
        "site:sec.gov",
        "site:federalregister.gov",
        "site:congress.gov",
    ],
    "GEOPOLITICS": [
        "site:un.org",
        "site:state.gov",
    ],
    "CRYPTO": [
        "site:coindesk.com",
        "site:defillama.com",
    ],
    "SPORTS": [],
    "ENTERTAINMENT": [],
}


@dataclass
class SearchQuery:
    """A search query with intent metadata."""
    text: str
    intent: str  # "primary" | "news" | "statistics" | "contrarian" | "confirmation"
    priority: int = 1  # 1 = highest


def build_queries(
    market: GammaMarket,
    max_queries: int = 8,
    category: Optional[str] = None,
    researchability: Optional[int] = None,
) -> list[SearchQuery]:
    """Generate search queries for a market.

    Args:
        market: The market to build queries for.
        max_queries: Hard cap on queries returned.
        category: Classifier category (e.g. "MACRO") — used for
                  site-restricted lookups instead of market.market_type.
        researchability: 0-100 score from classifier.  Controls budget:
                         LOW (<40)  → max 2 queries
                         NORMAL     → max 4 queries
                         HIGH (≥70) → up to max_queries
    """
    question = market.question.strip().rstrip("?")
    core = re.sub(r"^(Will|Is|Does|Has|Are|Do|Can|Should)\s+", "", question, flags=re.I)

    # ── Tiered budget based on researchability ───────────────────────
    if researchability is not None:
        if researchability < 40:
            max_queries = min(max_queries, 2)
        elif researchability < 70:
            max_queries = min(max_queries, 4)
        # else: use full max_queries

    queries: list[SearchQuery] = []

    # ── 1. Site-restricted primary source queries ────────────────────
    # Prefer classifier category, fall back to market.market_type
    lookup_key = category or market.market_type or ""
    site_restrictions = _SITE_RESTRICTED.get(lookup_key, [])
    for site in site_restrictions[:2]:  # Max 2 site-restricted queries
        queries.append(SearchQuery(
            text=f"{site} {core}",
            intent="primary",
            priority=1,
        ))

    # ── 2. Exact metric search ──────────────────────────────────────
    queries.append(SearchQuery(
        text=f'"{core}" official data release 2026',
        intent="statistics",
        priority=1,
    ))

    # ── 3. Recent news from major outlets ────────────────────────────
    queries.append(SearchQuery(
        text=f"{core} latest news 2026",
        intent="news",
        priority=2,
    ))

    # ── 4. Probability / forecast context ────────────────────────────
    if max_queries > 4:
        queries.append(SearchQuery(
            text=f"{core} probability forecast prediction analysis",
            intent="confirmation",
            priority=2,
        ))

    # ── 5. Contrarian / opposing view (only for high-budget) ─────────
    if max_queries > 5:
        queries.append(SearchQuery(
            text=f"{core} unlikely reasons against criticism",
            intent="contrarian",
            priority=3,
        ))

    # ── 6. Category-specific refinement (only for high-budget) ───────
    if max_queries > 6 and market.category and market.category.lower() not in core.lower():
        queries.append(SearchQuery(
            text=f"{market.category} {core}",
            intent="confirmation",
            priority=3,
        ))

    # Sort by priority and trim
    queries.sort(key=lambda q: q.priority)
    result = queries[:max_queries]

    log.info(
        "query_builder.built",
        market_id=market.id,
        category=lookup_key,
        researchability=researchability,
        num_queries=len(result),
        intents=[q.intent for q in result],
    )
    return result

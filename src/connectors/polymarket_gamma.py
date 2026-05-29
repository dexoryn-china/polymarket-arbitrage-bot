"""Polymarket Gamma (REST) API connector.

Gamma is Polymarket's public market-listing API. We use it to discover
markets, pull metadata (question, category, end date, resolution source),
and get basic pricing snapshots.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.observability.logger import get_logger
from src.connectors.rate_limiter import rate_limiter

log = get_logger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# ── Market type classification keywords ──────────────────────────────
_TYPE_KEYWORDS: dict[str, list[str]] = {
    "MACRO": [
        "cpi", "inflation", "unemployment", "gdp", "interest rate", "fed",
        "fomc", "ecb", "bls", "nonfarm", "payroll", "pce", "treasury",
        "yield", "rate cut", "rate hike", "recession", "jobs report",
    ],
    "ELECTION": [
        "election", "vote", "president", "governor", "senate", "congress",
        "primary", "nominee", "ballot", "electoral", "poll", "caucus",
    ],
    "CORPORATE": [
        "ipo", "merger", "acquisition", "sec", "earnings", "stock",
        "company", "ceo", "board", "filing", "shares", "revenue",
        "fda approval", "antitrust",
    ],
    "WEATHER": [
        "hurricane", "temperature", "noaa", "storm", "weather", "climate",
        "wildfire", "flood", "earthquake", "tornado",
    ],
    "SPORTS": [
        "super bowl", "nfl", "nba", "mlb", "world cup", "olympics",
        "championship", "playoffs", "mvp", "score",
    ],
}


# ── Data Models ──────────────────────────────────────────────────────

class GammaToken(BaseModel):
    """A single outcome token inside a market."""
    token_id: str = ""
    outcome: str = ""
    price: float = 0.0
    winner: bool | None = None


class GammaMarket(BaseModel):
    """Parsed representation of a Gamma market."""
    id: str = ""
    condition_id: str = ""
    question: str = ""
    description: str = ""
    category: str = ""
    market_type: str = ""  # MACRO | ELECTION | CORPORATE | WEATHER | SPORTS | UNKNOWN
    end_date: dt.datetime | None = None
    created_at: dt.datetime | None = None  # When the market started trading
    active: bool = True
    closed: bool = False
    volume: float = 0.0
    liquidity: float = 0.0
    tokens: list[GammaToken] = Field(default_factory=list)
    resolution_source: str = ""
    slug: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def age_hours(self) -> float | None:
        """Hours since the market started trading.  None if unknown."""
        if self.created_at is None:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        ca = self.created_at
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (now - ca).total_seconds() / 3600.0)

    @property
    def best_bid(self) -> float:
        """Highest Yes-token price (proxy for implied probability).

        Falls back to the first token's price if no 'Yes' outcome exists.
        """
        yes_tokens = [t for t in self.tokens if t.outcome.lower() == "yes"]
        if yes_tokens:
            return yes_tokens[0].price
        # For non-Yes/No markets, use the first token's price
        if self.tokens:
            return self.tokens[0].price
        return 0.0

    @property
    def spread(self) -> float:
        """Rough spread estimate from token prices."""
        if len(self.tokens) < 2:
            return 1.0
        prices = sorted([t.price for t in self.tokens], reverse=True)
        return abs(1.0 - sum(prices))

    @property
    def has_clear_resolution(self) -> bool:
        """Does the market have a defined resolution source?"""
        return bool(self.resolution_source and len(self.resolution_source) > 5)


# ── Classification ───────────────────────────────────────────────────

def classify_market_type(question: str, category: str = "", description: str = "") -> str:
    """Classify a market into a type based on keywords."""
    text = f"{question} {category} {description}".lower()
    scores: dict[str, int] = {}
    for mtype, keywords in _TYPE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[mtype] = score
    if not scores:
        return "UNKNOWN"
    return max(scores, key=scores.get)  # type: ignore[arg-type]


# ── Client ───────────────────────────────────────────────────────────

class GammaClient:
    """Async client for the Polymarket Gamma API."""

    def __init__(self, base_url: str = GAMMA_BASE, timeout: float = 30.0):
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await rate_limiter.get("gamma").acquire()
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def list_markets(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume",
        ascending: bool = False,
        category: str | None = None,
    ) -> list[GammaMarket]:
        """Fetch a page of markets from Gamma."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if category:
            params["tag"] = category

        data = await self._get("/markets", params=params)
        markets: list[GammaMarket] = []
        for raw in (data if isinstance(data, list)
                     else data.get("data", data.get("markets", []))):
            markets.append(parse_market(raw))
        log.info("gamma.list_markets", count=len(markets), offset=offset)
        return markets

    async def get_market(self, market_id: str) -> GammaMarket:
        """Fetch a single market by its condition-id or slug."""
        data = await self._get(f"/markets/{market_id}")
        mkt = parse_market(data)
        log.info("gamma.get_market", market_id=market_id, question=mkt.question[:80])
        return mkt


# ── Convenience helpers ──────────────────────────────────────────────

async def fetch_active_markets(
    *,
    min_volume: float = 0.0,
    limit: int = 100,
) -> list[GammaMarket]:
    """Fetch active markets, optionally filtering by minimum volume.

    Fetches two batches — one sorted by volume (established markets) and
    one sorted by newest first — then de-duplicates so brand-new markets
    are never missed.
    """
    client = GammaClient()
    try:
        # Batch 1: highest-volume markets (established liquidity)
        by_volume = await client.list_markets(
            limit=limit, active=True, closed=False, order="volume",
        )
        # Batch 2: newest markets (fresh opportunities)
        by_newest = await client.list_markets(
            limit=limit, active=True, closed=False, order="startDate",
        )
        # Merge & deduplicate (preserve order: newest first, then volume)
        seen: set[str] = set()
        merged: list[GammaMarket] = []
        for m in by_newest + by_volume:
            if m.id not in seen:
                seen.add(m.id)
                merged.append(m)
        if min_volume > 0:
            merged = [m for m in merged if m.volume >= min_volume]
        return merged
    finally:
        await client.close()


# ── Parsing helpers ──────────────────────────────────────────────────

def _parse_json_str(val: Any) -> list[Any]:
    """Parse a JSON-encoded string or return as-is if already a list."""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def parse_market(raw: dict[str, Any]) -> GammaMarket:
    """Convert a raw Gamma JSON blob into a GammaMarket."""
    tokens: list[GammaToken] = []

    # The Gamma list endpoint returns tokens data as JSON strings:
    #   outcomes:      '["Yes", "No"]'
    #   outcomePrices: '["0.55", "0.45"]'
    #   clobTokenIds:  '["123...", "456..."]'
    # The single-market endpoint may return a proper tokens array.
    raw_tokens = raw.get("tokens", [])
    if isinstance(raw_tokens, list) and raw_tokens and isinstance(raw_tokens[0], dict):
        # Already structured token objects (single-market endpoint)
        for tok in raw_tokens:
            tokens.append(
                GammaToken(
                    token_id=str(tok.get("token_id", tok.get("id", ""))),
                    outcome=tok.get("outcome", tok.get("value", "")),
                    price=float(tok.get("price", 0)),
                    winner=tok.get("winner"),
                )
            )
    else:
        # List endpoint — reconstruct tokens from separate JSON string fields
        outcomes = _parse_json_str(raw.get("outcomes", []))
        prices = _parse_json_str(raw.get("outcomePrices", []))
        clob_ids = _parse_json_str(raw.get("clobTokenIds", []))

        for i, outcome in enumerate(outcomes):
            price = float(prices[i]) if i < len(prices) else 0.0
            token_id = str(clob_ids[i]) if i < len(clob_ids) else ""
            tokens.append(
                GammaToken(
                    token_id=token_id,
                    outcome=str(outcome),
                    price=price,
                )
            )

    end_raw = raw.get("end_date_iso") or raw.get("end_date") or raw.get("endDate")
    end_date = None
    if end_raw:
        try:
            end_date = dt.datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    # ── Market creation / start date ─────────────────────────────
    # Prefer startDate (when trading opened) > acceptingOrdersTimestamp > createdAt
    created_raw = (
        raw.get("startDate")
        or raw.get("acceptingOrdersTimestamp")
        or raw.get("createdAt")
    )
    created_at = None
    if created_raw:
        try:
            created_at = dt.datetime.fromisoformat(
                str(created_raw).replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            pass

    question = raw.get("question", raw.get("title", ""))
    category = raw.get("category", raw.get("tag", ""))
    description = raw.get("description", "")
    market_type = classify_market_type(question, category, description)

    return GammaMarket(
        id=str(raw.get("id", raw.get("condition_id", ""))),
        condition_id=str(raw.get("condition_id", raw.get("conditionId", ""))),
        question=question,
        description=description,
        category=category,
        market_type=market_type,
        end_date=end_date,
        created_at=created_at,
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        volume=float(raw.get("volume", raw.get("volumeNum", 0))),
        liquidity=float(raw.get("liquidity", raw.get("liquidityNum", 0))),
        tokens=tokens,
        resolution_source=raw.get("resolution_source", raw.get("resolutionSource", "")),
        slug=raw.get("slug", ""),
        raw=raw,
    )

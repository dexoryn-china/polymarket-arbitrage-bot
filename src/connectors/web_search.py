"""Web search connector with pluggable backends.

Supported providers:
  - serpapi  (default, requires SERPAPI_KEY)
  - bing     (requires BING_API_KEY)
  - tavily   (requires TAVILY_API_KEY)

Includes domain whitelisting/blocking per the research agent spec.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.observability.logger import get_logger
from src.connectors.rate_limiter import rate_limiter
from src.observability.metrics import cost_tracker

log = get_logger(__name__)


# ── Data Models ──────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """A single web search result."""
    title: str
    url: str
    snippet: str
    source: str = ""         # publisher / domain
    date: str = ""            # publication date if available
    position: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


# ── Domain filtering ────────────────────────────────────────────────

def is_domain_blocked(url: str, blocked: list[str]) -> bool:
    """Check if a URL's domain is on the blocked list."""
    try:
        domain = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(b.lower() in domain for b in blocked)


def score_domain_authority(url: str, primary: list[str], secondary: list[str]) -> float:
    """Score a URL's domain authority (0-1)."""
    try:
        domain = urlparse(url).netloc.lower()
    except Exception:
        return 0.3
    for p in primary:
        if p.lower() in domain:
            return 1.0
    for s in secondary:
        if s.lower() in domain:
            return 0.7
    if domain.endswith(".gov"):
        return 0.95
    if domain.endswith(".edu"):
        return 0.8
    return 0.4


# ── Abstract Provider ────────────────────────────────────────────────

class SearchProvider(abc.ABC):
    """Base class for web search providers."""

    @abc.abstractmethod
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        ...

    async def close(self) -> None:
        pass


# ── SerpAPI Provider ─────────────────────────────────────────────────

class SerpAPIProvider(SearchProvider):
    """Google search via SerpAPI with automatic key rotation."""

    def __init__(self, api_key: str | None = None):
        raw = api_key or os.environ.get("SERPAPI_KEY", "")
        self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self._keys:
            log.warning("serpapi.no_key", msg="SERPAPI_KEY not set; searches will fail")
            self._keys = [""]
        self._key_index = 0
        self._client = httpx.AsyncClient(timeout=20.0)

    @property
    def _key(self) -> str:
        return self._keys[self._key_index]

    def _rotate_key(self) -> bool:
        """Rotate to next key. Returns True if a new key is available."""
        next_idx = self._key_index + 1
        if next_idx < len(self._keys):
            self._key_index = next_idx
            log.info("serpapi.key_rotated", key_index=next_idx, total_keys=len(self._keys))
            return True
        return False

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        await rate_limiter.get("serpapi").acquire()
        resp = await self._client.get(
            "https://serpapi.com/search.json",
            params={
                "q": query,
                "api_key": self._key,
                "num": num_results,
                "engine": "google",
            },
        )
        # On rate limit, try rotating to next key before raising
        if resp.status_code == 429 and self._rotate_key():
            log.warning("serpapi.rate_limited", msg="Retrying with rotated key")
            resp = await self._client.get(
                "https://serpapi.com/search.json",
                params={
                    "q": query,
                    "api_key": self._key,
                    "num": num_results,
                    "engine": "google",
                },
            )
        resp.raise_for_status()
        data = resp.json()
        results: list[SearchResult] = []
        for i, item in enumerate(data.get("organic_results", [])):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    source=item.get("source", item.get("displayed_link", "")),
                    date=item.get("date", ""),
                    position=i + 1,
                    raw=item,
                )
            )
        log.info("serpapi.search", query=query[:80], results=len(results))
        cost_tracker.record_call("serpapi")
        return results


# ── Bing Provider ────────────────────────────────────────────────────

class BingProvider(SearchProvider):
    """Bing Web Search API v7."""

    def __init__(self, api_key: str | None = None):
        self._key = api_key or os.environ.get("BING_API_KEY", "")
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        await rate_limiter.get("bing").acquire()
        resp = await self._client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            headers={"Ocp-Apim-Subscription-Key": self._key},
            params={"q": query, "count": num_results, "mkt": "en-US"},
        )
        resp.raise_for_status()
        data = resp.json()
        results: list[SearchResult] = []
        for i, item in enumerate(data.get("webPages", {}).get("value", [])):
            results.append(
                SearchResult(
                    title=item.get("name", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                    source=item.get("displayUrl", ""),
                    date=item.get("dateLastCrawled", ""),
                    position=i + 1,
                    raw=item,
                )
            )
        log.info("bing.search", query=query[:80], results=len(results))
        cost_tracker.record_call("bing")
        return results


# ── Tavily Provider ──────────────────────────────────────────────────

class TavilyProvider(SearchProvider):
    """Tavily AI search API with automatic key rotation."""

    def __init__(self, api_key: str | None = None):
        raw = api_key or os.environ.get("TAVILY_API_KEY", "")
        self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self._keys:
            log.warning("tavily.no_key", msg="TAVILY_API_KEY not set; searches will fail")
            self._keys = [""]
        self._key_index = 0
        self._client = httpx.AsyncClient(timeout=20.0)

    @property
    def _key(self) -> str:
        return self._keys[self._key_index]

    def _rotate_key(self) -> bool:
        """Rotate to next key. Returns True if a new key is available."""
        next_idx = self._key_index + 1
        if next_idx < len(self._keys):
            self._key_index = next_idx
            log.info("tavily.key_rotated", key_index=next_idx, total_keys=len(self._keys))
            return True
        return False

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        await rate_limiter.get("tavily").acquire()
        resp = await self._client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self._key,
                "query": query,
                "max_results": num_results,
                "search_depth": "advanced",
                "include_answer": False,
            },
        )
        # On rate limit or auth error, try rotating to next key before raising
        if resp.status_code in (429, 401, 403) and self._rotate_key():
            log.warning("tavily.rate_limited", msg="Retrying with rotated key")
            resp = await self._client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self._key,
                    "query": query,
                    "max_results": num_results,
                    "search_depth": "advanced",
                    "include_answer": False,
                },
            )
        resp.raise_for_status()
        data = resp.json()
        results: list[SearchResult] = []
        for i, item in enumerate(data.get("results", [])):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    source=(
                        item.get("url", "").split("/")[2]
                        if "/" in item.get("url", "") else ""
                    ),
                    date="",
                    position=i + 1,
                    raw=item,
                )
            )
        log.info("tavily.search", query=query[:80], results=len(results))
        cost_tracker.record_call("tavily")
        return results


# ── Factory ──────────────────────────────────────────────────────────

_PROVIDERS: dict[str, type[SearchProvider]] = {
    "serpapi": SerpAPIProvider,
    "bing": BingProvider,
    "tavily": TavilyProvider,
}


class FallbackSearchProvider(SearchProvider):
    """Search provider that tries multiple backends in order.

    If the primary provider fails (429, timeout, auth error), it
    automatically falls through to the next available provider.
    Default chain: serpapi → tavily.
    """

    def __init__(self, chain: list[str] | None = None):
        if chain is None:
            chain = ["serpapi", "tavily"]
        self._chain: list[SearchProvider] = []
        for name in chain:
            cls = _PROVIDERS.get(name.lower())
            if cls:
                self._chain.append(cls())
        if not self._chain:
            self._chain.append(SerpAPIProvider())

    async def close(self) -> None:
        for provider in self._chain:
            await provider.close()

    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        last_error: Exception | None = None
        for i, provider in enumerate(self._chain):
            try:
                results = await provider.search(query, num_results)
                if results:
                    return results
            except Exception as e:
                provider_name = type(provider).__name__
                log.warning(
                    "search.fallback",
                    provider=provider_name,
                    error=str(e),
                    next_provider=(
                        type(self._chain[i + 1]).__name__
                        if i + 1 < len(self._chain) else "none"
                    ),
                )
                last_error = e
                continue
        if last_error:
            log.error("search.all_providers_failed", error=str(last_error))
        return []


def create_search_provider(name: str = "serpapi") -> SearchProvider:
    """Create a search provider by name.

    Use "fallback" for automatic fallback chain (serpapi → bing → tavily).
    """
    if name.lower() == "fallback":
        return FallbackSearchProvider()
    cls = _PROVIDERS.get(name.lower())
    if cls is None:
        raise ValueError(
            f"Unknown search provider: {name!r}. Choose from: {list(_PROVIDERS) + ['fallback']}"
        )
    return cls()

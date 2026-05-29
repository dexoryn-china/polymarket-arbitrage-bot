"""Source fetcher — runs search queries and fetches page content.

Orchestrates:
  1. Executing search queries via the web_search connector
  2. Filtering out blocked domains
  3. De-duplicating results across queries
  4. Scoring sources by authority (primary > secondary > unknown)
  5. Fetching FULL page content for top sources (not just snippets)
  6. Extracting readable text via BeautifulSoup
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from src.config import ResearchConfig, CacheConfig
from src.connectors.web_search import (
    SearchProvider,
    SearchResult,
    is_domain_blocked,
    score_domain_authority,
)
from src.observability.logger import get_logger
from src.research.query_builder import SearchQuery
from src.storage.cache import get_cache, make_cache_key

log = get_logger(__name__)


@dataclass
class FetchedSource:
    """A source with full metadata and fetched content."""
    title: str
    url: str
    snippet: str
    publisher: str = ""
    date: str = ""
    content: str = ""           # full page text (if fetched)
    authority_score: float = 0.0
    query_intent: str = ""
    extraction_method: str = "search"  # "search" | "html" | "api"
    content_length: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class SourceFetcher:
    """Fetch and rank sources for a set of search queries."""

    # Shared search cache (2-hour TTL by default)
    _search_cache = get_cache("search", max_size_mb=50)
    _SEARCH_TTL_SECS = 7200  # 2 hours

    def __init__(self, provider: SearchProvider, config: ResearchConfig):
        self._provider = provider
        self._config = config
        self._http = httpx.AsyncClient(
            timeout=config.source_timeout_secs,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PolymarketBot/0.2)"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def fetch_sources(
        self,
        queries: list[SearchQuery],
        market_type: str = "UNKNOWN",
        max_sources: int | None = None,
    ) -> list[FetchedSource]:
        """Run all queries, filter, de-dup, rank, fetch content, return top sources."""
        max_sources = max_sources or self._config.max_sources
        seen_urls: set[str] = set()
        all_sources: list[FetchedSource] = []

        primary = self._config.primary_domains.get(market_type, [])
        secondary = self._config.secondary_domains
        blocked = self._config.blocked_domains

        # Run queries concurrently
        tasks = [self._run_query(q) for q in queries]
        results_per_query = await asyncio.gather(*tasks, return_exceptions=True)

        for query, results in zip(queries, results_per_query):
            if isinstance(results, BaseException):
                log.warning(
                    "source_fetcher.query_failed",
                    query=query.text[:80],
                    error=str(results),
                )
                continue
            for sr in results:
                if is_domain_blocked(sr.url, blocked):
                    continue

                canonical = _canonical_url(sr.url)
                if canonical in seen_urls:
                    continue
                seen_urls.add(canonical)

                all_sources.append(
                    FetchedSource(
                        title=sr.title,
                        url=sr.url,
                        snippet=sr.snippet,
                        publisher=sr.source or _extract_domain(sr.url),
                        date=sr.date,
                        authority_score=score_domain_authority(
                            sr.url, primary, secondary
                        ),
                        query_intent=query.intent,
                        raw=sr.raw,
                    )
                )

        # Sort: authority desc, then intent priority
        intent_order = {
            "primary": 0, "statistics": 1, "news": 2,
            "confirmation": 3, "contrarian": 4,
        }
        all_sources.sort(
            key=lambda s: (-s.authority_score, intent_order.get(s.query_intent, 5)),
        )
        top = all_sources[:max_sources]

        # Fetch full page content for top N sources
        if self._config.fetch_full_content:
            fetch_n = min(self._config.content_fetch_top_n, len(top))
            content_tasks = [
                self.fetch_page_content(src.url) for src in top[:fetch_n]
            ]
            contents = await asyncio.gather(*content_tasks, return_exceptions=True)
            for i, content in enumerate(contents):
                if isinstance(content, str) and content:
                    top[i].content = content
                    top[i].content_length = len(content)
                    top[i].extraction_method = "html"
                    log.debug(
                        "source_fetcher.content_fetched",
                        url=top[i].url[:80],
                        length=len(content),
                    )

        log.info(
            "source_fetcher.fetched",
            total_raw=len(all_sources),
            returned=len(top),
            with_content=sum(1 for s in top if s.content),
        )
        return top

    async def _run_query(self, query: SearchQuery) -> list[SearchResult]:
        # Check cache first
        cache_key = make_cache_key("search", query.text)
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            log.debug("source_fetcher.cache_hit", query=query.text[:60])
            return [SearchResult(**r) for r in cached]

        # Primary queries get full results; secondary/contrarian get fewer
        num = 5 if query.intent in ("primary", "statistics") else 3
        results = await self._provider.search(query.text, num_results=num)

        # Cache the results
        serialisable = [
            {"title": r.title, "url": r.url, "snippet": r.snippet,
             "source": r.source, "date": r.date, "position": r.position,
             "raw": r.raw}
            for r in results
        ]
        self._search_cache.put(cache_key, serialisable, self._SEARCH_TTL_SECS)
        return results

    async def fetch_page_content(self, url: str) -> str:
        """Fetch and extract readable text content from a URL using BeautifulSoup."""
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            html = resp.text

            # Try BeautifulSoup for better extraction
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "lxml")

                # Remove noise elements
                for tag in soup(["script", "style", "nav", "footer", "header",
                                 "aside", "noscript", "iframe", "svg"]):
                    tag.decompose()

                # Try to find main content
                main = (
                    soup.find("article")
                    or soup.find("main")
                    or soup.find(class_=re.compile(r"article|content|post|entry", re.I))
                    or soup.find("body")
                )
                if main:
                    text = main.get_text(separator="\n", strip=True)
                else:
                    text = soup.get_text(separator="\n", strip=True)

            except ImportError:
                # Fallback to regex stripping
                text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
                text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()

            # Clean up whitespace
            lines = [line.strip() for line in text.split("\n")]
            lines = [line for line in lines if len(line) > 20]  # Remove short noise lines
            text = "\n".join(lines)

            max_len = self._config.max_content_length
            return text[:max_len]

        except Exception as e:
            log.warning("source_fetcher.page_fetch_failed", url=url[:80], error=str(e))
            return ""


def _extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def _canonical_url(url: str) -> str:
    """Normalize URL for de-duplication."""
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}".rstrip("/").lower()

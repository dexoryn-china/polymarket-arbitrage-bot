"""Evidence extractor — pulls structured evidence from fetched sources.

Uses an LLM to:
  1. Extract key numeric facts with dates, units, and sources
  2. Identify the most relevant evidence bullets with citations
  3. Detect contradictions between sources
  4. Assess overall evidence quality
  5. Determine confidence score

ALSO performs independent quality scoring (not just LLM self-assessment):
  - Source recency penalty
  - Domain authority weighting
  - Cross-source agreement scoring
  - Numeric evidence density bonus

Implements strict extraction rules:
  - ONLY extract: numbers, official statements, dates, direct quotes
  - Every fact must have: metric name, value, unit, date, source, URL
  - If sources conflict: list both, reduce confidence
"""

from __future__ import annotations

import json
import re
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from src.config import ForecastingConfig, ResearchConfig
from src.observability.logger import get_logger
from src.research.source_fetcher import FetchedSource

log = get_logger(__name__)


@dataclass
class Citation:
    """A source citation."""
    url: str
    publisher: str
    date: str
    title: str = ""


@dataclass
class EvidenceBullet:
    """A single piece of evidence with citation."""
    text: str
    citation: Citation
    relevance: float = 0.0
    is_numeric: bool = False
    metric_name: str = ""
    metric_value: str = ""
    metric_unit: str = ""
    metric_date: str = ""
    confidence: float = 0.5


@dataclass
class Contradiction:
    """When two sources disagree."""
    claim_a: str
    source_a: Citation
    claim_b: str
    source_b: Citation
    description: str = ""


@dataclass
class IndependentQualityScore:
    """Quality score computed independently of LLM self-assessment."""
    overall: float = 0.0
    recency_score: float = 0.0
    authority_score: float = 0.0
    agreement_score: float = 0.0
    numeric_density_score: float = 0.0
    content_depth_score: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class EvidencePackage:
    """Complete evidence package for a market."""
    market_id: str
    question: str
    market_type: str = "UNKNOWN"
    bullets: list[EvidenceBullet] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    quality_score: float = 0.0
    llm_quality_score: float = 0.0
    independent_quality: IndependentQualityScore = field(
        default_factory=IndependentQualityScore
    )
    num_sources: int = 0
    summary: str = ""
    raw_llm_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "market_id": self.market_id,
            "question": self.question,
            "market_type": self.market_type,
            "evidence": [
                {
                    "text": b.text,
                    "citation": {
                        "url": b.citation.url,
                        "publisher": b.citation.publisher,
                        "date": b.citation.date,
                        "title": b.citation.title,
                    },
                    "relevance": b.relevance,
                    "is_numeric": b.is_numeric,
                    "metric_name": b.metric_name,
                    "metric_value": b.metric_value,
                    "metric_unit": b.metric_unit,
                    "metric_date": b.metric_date,
                    "confidence": b.confidence,
                }
                for b in self.bullets
            ],
            "contradictions": [
                {
                    "claim_a": c.claim_a,
                    "source_a_url": c.source_a.url,
                    "claim_b": c.claim_b,
                    "source_b_url": c.source_b.url,
                    "description": c.description,
                }
                for c in self.contradictions
            ],
            "quality_score": self.quality_score,
            "llm_quality_score": self.llm_quality_score,
            "independent_quality": {
                "overall": self.independent_quality.overall,
                "recency": self.independent_quality.recency_score,
                "authority": self.independent_quality.authority_score,
                "agreement": self.independent_quality.agreement_score,
                "numeric_density": self.independent_quality.numeric_density_score,
                "content_depth": self.independent_quality.content_depth_score,
            },
            "num_sources": self.num_sources,
            "summary": self.summary,
        }


def compute_independent_quality(
    sources: list[FetchedSource],
    bullets: list[EvidenceBullet],
    contradictions: list[Contradiction],
    stale_threshold_days: int = 7,
    heavy_stale_days: int = 30,
) -> IndependentQualityScore:
    """Compute evidence quality independently of LLM self-assessment.

    Scoring dimensions:
      1. Recency: are sources recent? (penalty for stale data)
      2. Authority: weighted average of source authority scores
      3. Agreement: do multiple sources agree? (contradiction penalty)
      4. Numeric density: more numbers = more verifiable = higher quality
      5. Content depth: did we get full content or just snippets?
    """
    if not sources:
        return IndependentQualityScore()

    now = dt.datetime.now(dt.timezone.utc)

    # 1. Recency score
    recency_scores = []
    for src in sources:
        if src.date:
            try:
                # Try various date formats
                date_str = src.date.strip()
                src_date = None
                for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y", "%B %d, %Y"]:
                    try:
                        src_date = dt.datetime.strptime(date_str[:19], fmt)
                        src_date = src_date.replace(tzinfo=dt.timezone.utc)
                        break
                    except ValueError:
                        continue
                if src_date:
                    age_days = (now - src_date).days
                    if age_days <= stale_threshold_days:
                        recency_scores.append(1.0)
                    elif age_days <= heavy_stale_days:
                        recency_scores.append(0.5)
                    else:
                        recency_scores.append(0.2)
                else:
                    recency_scores.append(0.4)  # Unknown date
            except Exception:
                recency_scores.append(0.4)
        else:
            recency_scores.append(0.4)
    recency = sum(recency_scores) / len(recency_scores) if recency_scores else 0.4

    # 2. Authority score (weighted avg)
    auth_scores = [src.authority_score for src in sources if src.authority_score > 0]
    authority = sum(auth_scores) / len(auth_scores) if auth_scores else 0.3
    # Bonus if any .gov or top-tier source
    has_gov = any(s.authority_score >= 0.95 for s in sources)
    if has_gov:
        authority = min(1.0, authority + 0.15)

    # 3. Agreement score
    n_contradictions = len(contradictions)
    n_sources = len(sources)
    agreement = 1.0
    if n_contradictions > 0:
        agreement = max(0.2, 1.0 - (n_contradictions * 0.15))

    # 4. Numeric density
    n_numeric = sum(1 for b in bullets if b.is_numeric)
    n_bullets = len(bullets) if bullets else 1
    numeric_density = min(1.0, n_numeric / max(n_bullets, 1) + 0.2 * min(n_numeric, 5))

    # 5. Content depth (full content vs snippets only)
    sources_with_content = sum(1 for s in sources if len(s.content) > 500)
    content_depth = min(1.0, sources_with_content / max(n_sources, 1) * 1.5)

    # Overall = weighted combination
    overall = (
        recency * 0.20
        + authority * 0.30
        + agreement * 0.20
        + numeric_density * 0.15
        + content_depth * 0.15
    )

    return IndependentQualityScore(
        overall=round(overall, 3),
        recency_score=round(recency, 3),
        authority_score=round(authority, 3),
        agreement_score=round(agreement, 3),
        numeric_density_score=round(numeric_density, 3),
        content_depth_score=round(content_depth, 3),
        breakdown={
            "recency_weight": 0.20,
            "authority_weight": 0.30,
            "agreement_weight": 0.20,
            "numeric_weight": 0.15,
            "depth_weight": 0.15,
        },
    )


_EXTRACTION_PROMPT = """\
You are a precise research analyst extracting evidence for a prediction market.

MARKET QUESTION: {question}
MARKET TYPE: {market_type}

SOURCES:
{sources_block}

TASK:
Analyze all sources and extract structured evidence. Return valid JSON:
{{
  "bullets": [
    {{
      "text": "key fact or statistic with specific numbers/dates",
      "source_index": <int>,
      "relevance": <0.0-1.0>,
      "is_numeric": <bool>,
      "metric_name": "e.g. CPI YoY, unemployment rate",
      "metric_value": "e.g. 3.2",
      "metric_unit": "e.g. percent, USD, basis points",
      "metric_date": "e.g. 2026-01-15",
      "confidence": <0.0-1.0>
    }}
  ],
  "contradictions": [
    {{
      "claim_a": "claim from source A",
      "source_a_index": <int>,
      "claim_b": "contradicting claim from source B",
      "source_b_index": <int>,
      "description": "brief explanation of disagreement"
    }}
  ],
  "quality_score": <0.0-1.0>,
  "summary": "2-3 sentence summary of the evidence landscape"
}}

EXTRACTION RULES:
- Extract ONLY: numbers, official statements, dates, direct quotes.
- Every numeric fact MUST have metric_name, metric_value, metric_unit, metric_date.
- Prefer recent data. Mark stale data (>30 days) with lower confidence.
- quality_score: 1.0 = overwhelming authoritative evidence, 0.0 = no relevant evidence.
- If sources contradict: list ALL contradictions AND lower quality_score by 0.1 per contradiction.
- If no authoritative primary source found: quality_score <= 0.3.
- Include at least the top 5 most relevant bullets.
- source_index = 0-based index of the source above.
- Do NOT fabricate data. If a source doesn't contain a number, don't invent one.

Return ONLY valid JSON, no markdown fences.
"""


class EvidenceExtractor:
    """Extract structured evidence from sources using an LLM."""

    def __init__(self, config: ForecastingConfig):
        self._config = config
        self._llm = AsyncOpenAI()

    async def extract(
        self,
        market_id: str,
        question: str,
        sources: list[FetchedSource],
        market_type: str = "UNKNOWN",
    ) -> EvidencePackage:
        """Extract evidence from a list of fetched sources."""
        if not sources:
            log.warning("evidence_extractor.no_sources", market_id=market_id)
            return EvidencePackage(
                market_id=market_id,
                question=question,
                market_type=market_type,
                quality_score=0.0,
                summary="No sources available for analysis.",
            )

        # Build sources block — include full content if available
        source_lines: list[str] = []
        for i, s in enumerate(sources):
            content_text = s.content[:3000] if s.content else s.snippet[:500]
            source_lines.append(
                f"[{i}] {s.title}\n"
                f"    URL: {s.url}\n"
                f"    Publisher: {s.publisher}\n"
                f"    Date: {s.date or 'unknown'}\n"
                f"    Authority: {s.authority_score:.1f}\n"
                f"    Content: {content_text}"
            )
        sources_block = "\n\n".join(source_lines)

        prompt = _EXTRACTION_PROMPT.format(
            question=question,
            market_type=market_type,
            sources_block=sources_block,
        )

        try:
            resp = await self._llm.chat.completions.create(
                model=self._config.llm_model,
                temperature=0.1,
                max_tokens=self._config.llm_max_tokens,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a precise research analyst. "
                            "Return only valid JSON. Never fabricate data."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            raw_text = resp.choices[0].message.content or "{}"
            raw_text = raw_text.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()

            parsed = json.loads(raw_text)
        except Exception as e:
            log.error("evidence_extractor.llm_failed", market_id=market_id, error=str(e))
            return EvidencePackage(
                market_id=market_id,
                question=question,
                market_type=market_type,
                quality_score=0.0,
                summary=f"LLM extraction failed: {e}",
            )

        return _build_package(market_id, question, market_type, sources, parsed)


def _build_package(
    market_id: str,
    question: str,
    market_type: str,
    sources: list[FetchedSource],
    parsed: dict[str, Any],
) -> EvidencePackage:
    """Build an EvidencePackage from parsed LLM output with independent quality."""
    bullets: list[EvidenceBullet] = []
    for b in parsed.get("bullets", []):
        idx = b.get("source_index", 0)
        src = sources[idx] if 0 <= idx < len(sources) else None
        citation = Citation(
            url=src.url if src else "",
            publisher=src.publisher if src else "",
            date=src.date if src else "",
            title=src.title if src else "",
        )
        bullets.append(
            EvidenceBullet(
                text=b.get("text", ""),
                citation=citation,
                relevance=float(b.get("relevance", 0.5)),
                is_numeric=bool(b.get("is_numeric", False)),
                metric_name=b.get("metric_name", ""),
                metric_value=b.get("metric_value", ""),
                metric_unit=b.get("metric_unit", ""),
                metric_date=b.get("metric_date", ""),
                confidence=float(b.get("confidence", 0.5)),
            )
        )

    contradictions: list[Contradiction] = []
    for c in parsed.get("contradictions", []):
        idx_a = c.get("source_a_index", 0)
        idx_b = c.get("source_b_index", 0)
        src_a = sources[idx_a] if 0 <= idx_a < len(sources) else None
        src_b = sources[idx_b] if 0 <= idx_b < len(sources) else None
        contradictions.append(
            Contradiction(
                claim_a=c.get("claim_a", ""),
                source_a=Citation(
                    url=src_a.url if src_a else "",
                    publisher=src_a.publisher if src_a else "",
                    date=src_a.date if src_a else "",
                ),
                claim_b=c.get("claim_b", ""),
                source_b=Citation(
                    url=src_b.url if src_b else "",
                    publisher=src_b.publisher if src_b else "",
                    date=src_b.date if src_b else "",
                ),
                description=c.get("description", ""),
            )
        )

    # LLM's own quality assessment
    llm_quality = float(parsed.get("quality_score", 0.0))

    # Independent quality scoring
    independent = compute_independent_quality(
        sources, bullets, contradictions,
    )

    # Final quality = blend of LLM assessment and independent scoring
    # Independent scoring gets more weight (60/40 split)
    final_quality = llm_quality * 0.4 + independent.overall * 0.6

    package = EvidencePackage(
        market_id=market_id,
        question=question,
        market_type=market_type,
        bullets=bullets,
        contradictions=contradictions,
        quality_score=round(final_quality, 3),
        llm_quality_score=llm_quality,
        independent_quality=independent,
        num_sources=len(sources),
        summary=parsed.get("summary", ""),
        raw_llm_response=parsed,
    )

    log.info(
        "evidence_extractor.extracted",
        market_id=market_id,
        bullets=len(bullets),
        contradictions=len(contradictions),
        llm_quality=llm_quality,
        independent_quality=independent.overall,
        final_quality=final_quality,
    )
    return package


def parse_evidence_from_raw(
    market_id: str,
    question: str,
    sources: list[FetchedSource],
    raw_json: dict[str, Any],
    market_type: str = "UNKNOWN",
) -> EvidencePackage:
    """Public helper to build an EvidencePackage from already-parsed JSON (for tests)."""
    return _build_package(market_id, question, market_type, sources, raw_json)

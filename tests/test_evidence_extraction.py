"""Tests for evidence extraction logic (non-LLM parsing)."""

from __future__ import annotations

from src.research.evidence_extractor import (
    parse_evidence_from_raw,
    Citation,
    EvidenceBullet,
    EvidencePackage,
)
from src.research.source_fetcher import FetchedSource


def _make_sources() -> list[FetchedSource]:
    return [
        FetchedSource(
            title="BLS CPI Report January 2026",
            url="https://www.bls.gov/news.release/cpi.nr0.htm",
            snippet="The CPI-U increased 3.1% over the last 12 months.",
            publisher="Bureau of Labor Statistics",
            date="2026-02-12",
            authority_score=1.0,
        ),
        FetchedSource(
            title="Reuters: Inflation ticks up",
            url="https://reuters.com/economy/inflation-jan-2026",
            snippet="U.S. consumer prices rose 3.1% annually in January.",
            publisher="Reuters",
            date="2026-02-12",
            authority_score=0.7,
        ),
        FetchedSource(
            title="Bloomberg Analysis: CPI outlook",
            url="https://bloomberg.com/cpi-analysis-2026",
            snippet="Economists expected a 2.9% reading, making the 3.1% a surprise.",
            publisher="Bloomberg",
            date="2026-02-13",
            authority_score=0.7,
        ),
    ]


class TestParseEvidenceFromRaw:
    """Test building EvidencePackage from parsed LLM JSON."""

    def test_basic_extraction(self) -> None:
        sources = _make_sources()
        raw_json = {
            "bullets": [
                {
                    "text": "CPI-U increased 3.1% YoY in January 2026",
                    "source_index": 0,
                    "relevance": 0.95,
                    "is_numeric": True,
                    "metric_name": "CPI-U YoY",
                    "metric_value": "3.1",
                    "metric_unit": "percent",
                    "metric_date": "2026-01-31",
                    "confidence": 0.95,
                },
                {
                    "text": "Economists expected 2.9%, actual was higher",
                    "source_index": 2,
                    "relevance": 0.7,
                    "is_numeric": True,
                    "metric_name": "CPI expectation",
                    "metric_value": "2.9",
                    "metric_unit": "percent",
                    "metric_date": "2026-01-31",
                    "confidence": 0.8,
                },
            ],
            "contradictions": [],
            "quality_score": 0.9,
            "summary": "Strong authoritative evidence from BLS confirms CPI at 3.1% YoY.",
        }

        package = parse_evidence_from_raw(
            market_id="test_123",
            question="Will CPI exceed 3%?",
            sources=sources,
            raw_json=raw_json,
        )

        assert package.market_id == "test_123"
        assert package.llm_quality_score == 0.9
        # Final quality is blended: 0.4 * LLM + 0.6 * independent
        assert 0.8 <= package.quality_score <= 1.0
        assert len(package.bullets) == 2
        assert package.bullets[0].text == "CPI-U increased 3.1% YoY in January 2026"
        assert package.bullets[0].is_numeric is True
        assert package.bullets[0].metric_name == "CPI-U YoY"
        assert package.bullets[0].metric_value == "3.1"
        assert package.bullets[0].citation.url == "https://www.bls.gov/news.release/cpi.nr0.htm"
        assert package.bullets[0].citation.publisher == "Bureau of Labor Statistics"
        assert len(package.contradictions) == 0

    def test_with_contradictions(self) -> None:
        sources = _make_sources()
        raw_json = {
            "bullets": [
                {
                    "text": "CPI at 3.1%",
                    "source_index": 0,
                    "relevance": 0.9,
                    "is_numeric": True,
                },
            ],
            "contradictions": [
                {
                    "claim_a": "CPI was 3.1%",
                    "source_a_index": 0,
                    "claim_b": "CPI was 2.8%",
                    "source_b_index": 1,
                    "description": "Sources disagree on exact figure",
                },
            ],
            "quality_score": 0.5,
            "summary": "Sources conflict on exact CPI value.",
        }

        package = parse_evidence_from_raw(
            market_id="test_456",
            question="CPI question",
            sources=sources,
            raw_json=raw_json,
        )

        assert len(package.contradictions) == 1
        assert package.contradictions[0].claim_a == "CPI was 3.1%"
        assert package.contradictions[0].source_a.publisher == "Bureau of Labor Statistics"
        assert package.contradictions[0].claim_b == "CPI was 2.8%"
        assert package.llm_quality_score == 0.5
        # Blended score: LLM says 0.5, independent evaluator may score higher
        assert package.quality_score < 0.8

    def test_out_of_range_source_index(self) -> None:
        """Handle source_index that's out of range."""
        sources = _make_sources()
        raw_json = {
            "bullets": [
                {
                    "text": "Some fact",
                    "source_index": 99,  # out of range
                    "relevance": 0.5,
                },
            ],
            "contradictions": [],
            "quality_score": 0.3,
            "summary": "Limited evidence.",
        }

        package = parse_evidence_from_raw(
            market_id="test_oob",
            question="Test",
            sources=sources,
            raw_json=raw_json,
        )

        assert len(package.bullets) == 1
        assert package.bullets[0].citation.url == ""  # No source found

    def test_empty_bullets(self) -> None:
        """Handle no evidence extracted."""
        package = parse_evidence_from_raw(
            market_id="test_empty",
            question="Test",
            sources=[],
            raw_json={
                "bullets": [],
                "contradictions": [],
                "quality_score": 0.0,
                "summary": "No relevant evidence found.",
            },
        )

        assert len(package.bullets) == 0
        assert package.quality_score == 0.0
        assert package.summary == "No relevant evidence found."

    def test_to_dict(self) -> None:
        """Test serialization to dict."""
        sources = _make_sources()
        raw_json = {
            "bullets": [
                {
                    "text": "CPI at 3.1%",
                    "source_index": 0,
                    "relevance": 0.9,
                    "is_numeric": True,
                    "metric_name": "CPI",
                    "metric_value": "3.1",
                    "metric_unit": "percent",
                    "metric_date": "2026-01",
                    "confidence": 0.95,
                },
            ],
            "contradictions": [],
            "quality_score": 0.85,
            "summary": "Clear evidence.",
        }

        package = parse_evidence_from_raw(
            market_id="test_dict",
            question="Test",
            sources=sources,
            raw_json=raw_json,
        )

        d = package.to_dict()
        assert d["market_id"] == "test_dict"
        # Blended quality score (0.4*LLM + 0.6*independent)
        assert 0.8 <= d["quality_score"] <= 0.9
        assert len(d["evidence"]) == 1
        assert d["evidence"][0]["metric_name"] == "CPI"
        assert d["evidence"][0]["citation"]["url"] == "https://www.bls.gov/news.release/cpi.nr0.htm"


class TestEvidencePackageNoSources:
    """Test edge case: evidence package with no sources."""

    def test_empty_package(self) -> None:
        package = EvidencePackage(
            market_id="empty",
            question="Test",
        )
        assert package.quality_score == 0.0
        assert package.num_sources == 0
        assert len(package.bullets) == 0

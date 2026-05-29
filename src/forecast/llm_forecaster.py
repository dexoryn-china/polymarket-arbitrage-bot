"""LLM forecaster — produces calibrated probability estimates.

Takes a MarketFeatures vector + EvidencePackage and asks the LLM
to produce a probability forecast with reasoning, invalidation
triggers, and confidence assessment.

Output format matches the strict JSON schema required by the bot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from src.config import ForecastingConfig
from src.forecast.feature_builder import MarketFeatures
from src.research.evidence_extractor import EvidencePackage
from src.observability.logger import get_logger
from src.connectors.rate_limiter import rate_limiter

log = get_logger(__name__)


@dataclass
class ForecastResult:
    """Complete forecast for a market."""
    market_id: str
    question: str
    market_type: str = "UNKNOWN"
    resolution_source: str = ""

    # Probabilities
    implied_probability: float = 0.5
    model_probability: float = 0.5
    edge: float = 0.0
    confidence_level: str = "LOW"  # LOW | MEDIUM | HIGH

    # Evidence
    evidence: list[dict[str, Any]] = field(default_factory=list)
    invalidation_triggers: list[str] = field(default_factory=list)
    reasoning: str = ""

    # Decision
    decision: str = "NO TRADE"  # TRADE | NO TRADE
    decision_reasons: list[str] = field(default_factory=list)

    # Meta
    evidence_quality: float = 0.0
    num_sources: int = 0
    raw_llm_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "market_type": self.market_type,
            "resolution_source": self.resolution_source,
            "implied_probability": round(self.implied_probability, 4),
            "model_probability": round(self.model_probability, 4),
            "edge": round(self.edge, 4),
            "confidence_level": self.confidence_level,
            "evidence": self.evidence,
            "invalidation_triggers": self.invalidation_triggers,
            "reasoning": self.reasoning,
            "decision": self.decision,
            "decision_reasons": self.decision_reasons,
            "evidence_quality": round(self.evidence_quality, 2),
            "num_sources": self.num_sources,
        }


_FORECAST_PROMPT = """\
You are an expert probabilistic forecaster analyzing a prediction market.

MARKET QUESTION: {question}
MARKET TYPE: {market_type}

EVIDENCE SUMMARY:
{evidence_summary}

TOP EVIDENCE BULLETS:
{evidence_bullets}

{contradictions_block}

MARKET FEATURES:
- Volume: ${volume_usd:,.0f}
- Liquidity: ${liquidity_usd:,.0f}
- Spread: {spread_pct:.1%}
- Days to expiry: {days_to_expiry:.0f}
- Price momentum (24h): {price_momentum:+.3f}
- Evidence quality score: {evidence_quality:.2f}
- Sources analyzed: {num_sources}

TASK:
Based ONLY on the evidence above, produce an independent probability
estimate for the question. Do NOT try to guess or anchor to any market
price — form your own view from the evidence.

Return valid JSON:
{{
  "model_probability": <0.01-0.99>,
  "confidence_level": "LOW" | "MEDIUM" | "HIGH",
  "reasoning": "2-4 sentence explanation of your probability estimate",
  "invalidation_triggers": [
    "specific event/data that would change this forecast significantly"
  ],
  "key_evidence": [
    {{
      "text": "evidence bullet",
      "source": "publisher name",
      "url": "source url",
      "date": "date",
      "impact": "supports/opposes/neutral"
    }}
  ]
}}

RULES:
- Your probability must be between 0.01 and 0.99 (never 0 or 1).
- Form your estimate independently from evidence — do NOT anchor to any
  external price or implied probability.
- If evidence is weak (quality < 0.3), bias toward 0.50 (maximum uncertainty).
- If evidence contradicts itself, widen uncertainty toward 0.50.
- confidence_level:
  - HIGH = authoritative primary source data directly answers the question
  - MEDIUM = strong secondary sources with consistent direction
  - LOW = limited/conflicting/stale evidence
- List 2-4 specific invalidation triggers.
- Never claim certainty. Express epistemic humility.
- Do NOT hallucinate data not present in the evidence.

Return ONLY valid JSON, no markdown fences.
"""


class LLMForecaster:
    """Generate probability forecasts using an LLM."""

    def __init__(self, config: ForecastingConfig):
        self._config = config
        self._llm = AsyncOpenAI()

    async def forecast(
        self,
        features: MarketFeatures,
        evidence: EvidencePackage,
        resolution_source: str = "",
    ) -> ForecastResult:
        """Generate a forecast for a market."""
        # Build evidence bullets text
        evidence_bullets = "\n".join(
            f"- {b}" for b in features.top_bullets
        ) if features.top_bullets else "No evidence bullets available."

        # Build contradictions block
        contradictions_block = ""
        if evidence.contradictions:
            lines = ["CONTRADICTIONS DETECTED:"]
            for c in evidence.contradictions:
                lines.append(
                    f"- {c.claim_a} ({c.source_a.publisher}) vs "
                    f"{c.claim_b} ({c.source_b.publisher}): {c.description}"
                )
            contradictions_block = "\n".join(lines)

        prompt = _FORECAST_PROMPT.format(
            question=features.question,
            market_type=features.market_type,
            evidence_summary=evidence.summary or "No summary available.",
            evidence_bullets=evidence_bullets,
            contradictions_block=contradictions_block,
            volume_usd=features.volume_usd,
            liquidity_usd=features.liquidity_usd,
            spread_pct=features.spread_pct,
            days_to_expiry=features.days_to_expiry,
            price_momentum=features.price_momentum,
            evidence_quality=features.evidence_quality,
            num_sources=features.num_sources,
        )

        try:
            await rate_limiter.get("openai").acquire()
            resp = await self._llm.chat.completions.create(
                model=self._config.llm_model,
                temperature=self._config.llm_temperature,
                max_tokens=self._config.llm_max_tokens,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a calibrated probabilistic forecaster. "
                            "You never claim certainty. You express epistemic humility. "
                            "Return only valid JSON."
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
            log.error("llm_forecaster.failed", market_id=features.market_id, error=str(e))
            return ForecastResult(
                market_id=features.market_id,
                question=features.question,
                market_type=features.market_type,
                implied_probability=features.implied_probability,
                model_probability=0.5,
                confidence_level="LOW",
                reasoning=f"LLM forecast failed: {e}",
                decision="NO TRADE",
                decision_reasons=["LLM failure"],
            )

        model_prob = max(0.01, min(0.99, float(parsed.get("model_probability", 0.5))))

        # Apply low-evidence penalty: pull toward 0.5
        if evidence.quality_score < self._config.min_evidence_quality:
            penalty = self._config.low_evidence_penalty
            model_prob = model_prob * (1 - penalty) + 0.5 * penalty
            log.info(
                "llm_forecaster.low_evidence_penalty",
                original=parsed.get("model_probability"),
                adjusted=model_prob,
            )

        edge = model_prob - features.implied_probability

        result = ForecastResult(
            market_id=features.market_id,
            question=features.question,
            market_type=features.market_type,
            resolution_source=resolution_source,
            implied_probability=features.implied_probability,
            model_probability=model_prob,
            edge=edge,
            confidence_level=parsed.get("confidence_level", "LOW"),
            evidence=parsed.get("key_evidence", []),
            invalidation_triggers=parsed.get("invalidation_triggers", []),
            reasoning=parsed.get("reasoning", ""),
            evidence_quality=evidence.quality_score,
            num_sources=evidence.num_sources,
            raw_llm_response=parsed,
        )

        log.info(
            "llm_forecaster.result",
            market_id=result.market_id,
            implied=round(result.implied_probability, 3),
            model=round(result.model_probability, 3),
            edge=round(result.edge, 3),
            confidence=result.confidence_level,
        )
        return result

"""Advanced market classifier with 11 categories, researchability scoring,
and adaptive research budgets.

This module replaces the simple keyword-matching ``classify_market_type()``
in ``polymarket_gamma.py`` with a much richer taxonomy.  Each market
receives:

  • category + subcategory  (e.g.  MACRO / fed_rates)
  • researchability score   (0-100)
  • recommended query budget (2-8)
  • primary data sources
  • search strategy label
  • semantic tags

The classifier is pure-Python with no external dependencies so it can be
evaluated at filter time *before* any web searches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from src.observability.logger import get_logger

log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════


@dataclass
class MarketClassification:
    """Rich classification result for a single market."""

    category: str              # e.g. "MACRO", "ELECTION", "CRYPTO"
    subcategory: str           # e.g. "fed_rates", "presidential"
    researchability: int       # 0-100 — how well can we research this
    researchability_reasons: List[str] = field(default_factory=list)
    primary_sources: List[str] = field(default_factory=list)
    search_strategy: str = ""  # "official_data", "news_analysis", "skip"
    recommended_queries: int = 4
    worth_researching: bool = True
    confidence: float = 0.8
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "subcategory": self.subcategory,
            "researchability": self.researchability,
            "researchability_reasons": self.researchability_reasons,
            "primary_sources": self.primary_sources,
            "search_strategy": self.search_strategy,
            "recommended_queries": self.recommended_queries,
            "worth_researching": self.worth_researching,
            "confidence": self.confidence,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MarketClassification":
        if not d or not isinstance(d, dict):
            return cls(category="UNKNOWN", subcategory="unknown",
                       researchability=30, worth_researching=False)
        return cls(
            category=d.get("category", "UNKNOWN"),
            subcategory=d.get("subcategory", "unknown"),
            researchability=d.get("researchability", 30),
            researchability_reasons=d.get("researchability_reasons", []),
            primary_sources=d.get("primary_sources", []),
            search_strategy=d.get("search_strategy", ""),
            recommended_queries=d.get("recommended_queries", 4),
            worth_researching=d.get("worth_researching", True),
            confidence=d.get("confidence", 0.5),
            tags=d.get("tags", []),
        )


# ═══════════════════════════════════════════════════════════════
#  CLASSIFICATION RULES
# ═══════════════════════════════════════════════════════════════
#
# Each rule is: (compiled_regex, category, subcategory, config_dict)
#
# config_dict keys:
#   researchability  – int 0-100
#   sources          – list of source labels
#   strategy         – search strategy string
#   queries          – recommended query count
#   tags             – list of tags
#   reasons          – researchability explanation strings

_ClassRule = Tuple[re.Pattern, str, str, Dict[str, Any]]

_RULES: List[_ClassRule] = []


def _r(pattern: str, cat: str, sub: str, **kw: Any) -> None:
    """Helper to register a classification rule."""
    _RULES.append((
        re.compile(pattern, re.IGNORECASE),
        cat, sub, kw,
    ))


# ── MACRO ────────────────────────────────────────────────────────────

_r(r"\b(fed(eral reserve)?|fomc|fed\s+fund|interest\s+rate|rate\s+(cut|hike|hold|pause|decision))\b",
   "MACRO", "fed_rates",
   researchability=92, sources=["federalreserve.gov", "CME FedWatch", "Reuters"],
   strategy="official_data", queries=8, tags=["scheduled_event", "high_signal"],
   reasons=["Official Fed calendar provides exact meeting dates",
            "CME FedWatch gives real-time market-implied probabilities"])

_r(r"\b(cpi|inflation|consumer\s+price|price\s+index|pce|core\s+inflation)\b",
   "MACRO", "inflation",
   researchability=90, sources=["bls.gov", "Cleveland Fed Nowcast", "Reuters"],
   strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
   reasons=["BLS releases CPI on fixed schedule",
            "Cleveland Fed Nowcast provides pre-release estimates"])

_r(r"\b(gdp|gross\s+domestic|economic\s+growth|gd[p]?\s+growth)\b",
   "MACRO", "gdp",
   researchability=88, sources=["bea.gov", "Atlanta Fed GDPNow", "Reuters"],
   strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
   reasons=["BEA advance/preliminary/final GDP reports on set schedule"])

_r(r"\b(unemployment|jobless|nonfarm\s+payroll|payrolls?|jobs?\s+report|employment\s+data|labor\s+market)\b",
   "MACRO", "employment",
   researchability=90, sources=["bls.gov", "ADP", "Reuters"],
   strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
   reasons=["BLS jobs report on first Friday of each month"])

_r(r"\b(tariffs?|trade\s+war|trade\s+(deal|agreement|deficit)|import\s+dut|export\s+ban)\b",
   "MACRO", "trade",
   researchability=78, sources=["ustr.gov", "Reuters", "Bloomberg"],
   strategy="news_analysis", queries=6, tags=["policy_dependent"],
   reasons=["Trade policy is politically driven — news coverage is good"])

_r(r"\b(recession|economic\s+downturn|soft\s+landing|hard\s+landing|inverted\s+yield)\b",
   "MACRO", "recession",
   researchability=75, sources=["NBER", "Federal Reserve", "Reuters"],
   strategy="news_analysis", queries=6, tags=["long_horizon", "composite_indicator"],
   reasons=["Recession declared retrospectively by NBER",
            "Leading indicators available but imperfect"])

_r(r"\b(treasury|bond\s+yield|yield\s+curve|10.year|2.year|t.bill)\b",
   "MACRO", "bonds",
   researchability=85, sources=["treasury.gov", "Bloomberg", "FRED"],
   strategy="official_data", queries=6, tags=["real_time_data"],
   reasons=["Treasury yields are publicly available real-time"])

# ── ELECTION ─────────────────────────────────────────────────────────

_r(r"\b(president(ial)?|white\s+house)\b.*\b(win|elect|nominee|race|202[4-9])\b|\b(win|elect|nominee|race|202[4-9])\b.*\b(president(ial)?|white\s+house)\b",
   "ELECTION", "presidential",
   researchability=88, sources=["FiveThirtyEight", "RCP", "AP News"],
   strategy="news_analysis", queries=8, tags=["polling_data", "high_signal"],
   reasons=["Extensive polling data available",
            "Major news coverage from multiple outlets"])

_r(r"\b(senate|senat|congress(ional)?|house\s+(of\s+)?rep|midterm)\b",
   "ELECTION", "congressional",
   researchability=82, sources=["Cook Political Report", "FiveThirtyEight", "Ballotpedia"],
   strategy="news_analysis", queries=6, tags=["polling_data"],
   reasons=["Good polling and historical data for most races"])

_r(r"\b(governor|mayor|state\s+(election|legislature)|ballot\s+measure|referendum)\b",
   "ELECTION", "state_local",
   researchability=68, sources=["Ballotpedia", "local news"],
   strategy="news_analysis", queries=5, tags=["limited_polling"],
   reasons=["State/local polling is sparser but exists"])

_r(r"\b(cabinet|appoint(ment|ed)?|nomin(ate|ation|ee)|confirm(ation|ed)?|secretary\s+of)\b",
   "ELECTION", "appointments",
   researchability=72, sources=["AP News", "Reuters", "Politico"],
   strategy="news_analysis", queries=5, tags=["political"],
   reasons=["Appointments are covered by political press"])

_r(r"\b(bill\s+pass|legislat(ion|ive)|act\s+(pass|sign|vote)|law\s+(pass|sign))\b",
   "ELECTION", "legislation",
   researchability=70, sources=["congress.gov", "Politico", "Reuters"],
   strategy="news_analysis", queries=5, tags=["legislative_tracking"],
   reasons=["Congress.gov tracks bill status",
            "GovTrack provides probability estimates"])

# General election fallback
_r(r"\b(election|vote|ballot|poll(ing)?|primary|caucus|electoral)\b",
   "ELECTION", "general",
   researchability=80, sources=["AP News", "Reuters", "FiveThirtyEight"],
   strategy="news_analysis", queries=6, tags=["polling_data"],
   reasons=["General election coverage widely available"])

# ── CRYPTO ───────────────────────────────────────────────────────────

_r(r"\b(bitcoin|btc)\b.{0,30}\b(price|reach|hit|above|below|\$|usd)\b",
   "CRYPTO", "btc_price",
   researchability=65, sources=["CoinGecko", "TradingView", "CoinDesk"],
   strategy="market_data", queries=5, tags=["volatile", "24_7_market"],
   reasons=["Real-time price data available",
            "High volatility makes predictions harder"])

_r(r"\b(ethereum|eth)\b.{0,30}\b(price|reach|hit|above|below|\$|usd)\b",
   "CRYPTO", "eth_price",
   researchability=62, sources=["CoinGecko", "TradingView", "CoinDesk"],
   strategy="market_data", queries=5, tags=["volatile", "24_7_market"],
   reasons=["Real-time price data available but very volatile"])

_r(r"\b(solana|sol|dogecoin|doge|xrp|cardano|ada|altcoin)\b.{0,30}\b(price|reach|hit|pump)\b",
   "CRYPTO", "altcoin_price",
   researchability=45, sources=["CoinGecko", "CoinMarketCap"],
   strategy="market_data", queries=3, tags=["volatile", "speculative"],
   reasons=["Altcoin prices extremely volatile", "Limited fundamental analysis possible"])

_r(r"\b(crypto\s+regulation|sec\s+(vs|sue|lawsuit|approve|etf)|bitcoin\s+etf|spot\s+etf)\b",
   "CRYPTO", "crypto_regulation",
   researchability=75, sources=["SEC.gov", "CoinDesk", "The Block"],
   strategy="news_analysis", queries=6, tags=["regulatory"],
   reasons=["SEC filings and court docs are public"])

_r(r"\b(bitcoin|crypto|ethereum)\b.{0,50}\b(halving|merge|upgrade|fork|launch)\b",
   "CRYPTO", "crypto_events",
   researchability=78, sources=["CoinDesk", "Ethereum.org", "GitHub"],
   strategy="news_analysis", queries=5, tags=["scheduled_event"],
   reasons=["Protocol upgrades have known schedules"])

# General crypto fallback
_r(r"\b(crypto|bitcoin|btc|ethereum|eth|blockchain|defi|nft\b)",
   "CRYPTO", "general",
   researchability=55, sources=["CoinDesk", "CoinGecko"],
   strategy="market_data", queries=4, tags=["volatile"],
   reasons=["Crypto markets are data-rich but volatile"])

# ── CORPORATE ────────────────────────────────────────────────────────

_r(r"\b(earnings|revenue|profit|quarterly\s+results|eps|beat\s+estimate|miss\s+estimate)\b",
   "CORPORATE", "earnings",
   researchability=85, sources=["SEC EDGAR", "Yahoo Finance", "Bloomberg"],
   strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
   reasons=["Earnings dates are known in advance",
            "Analyst consensus estimates widely available"])

_r(r"\b(ipo|initial\s+public\s+offering|go(ing)?\s+public|direct\s+listing|spac)\b",
   "CORPORATE", "ipo",
   researchability=72, sources=["SEC EDGAR", "IPO Monitor", "Bloomberg"],
   strategy="news_analysis", queries=5, tags=["corporate_action"],
   reasons=["IPO filings are public (S-1)"])

_r(r"\b(merger|acquisition|acquire|buyout|takeover|m&a|deal\s+close)\b",
   "CORPORATE", "mna",
   researchability=78, sources=["SEC EDGAR", "Reuters", "Bloomberg"],
   strategy="news_analysis", queries=6, tags=["corporate_action"],
   reasons=["M&A filings and regulatory approvals are public"])

_r(r"\b(layoff|workforce\s+reduction|hiring\s+freeze|job\s+cut|downsize|restructur)\b",
   "CORPORATE", "layoffs",
   researchability=70, sources=["Reuters", "Bloomberg", "WARN Act filings"],
   strategy="news_analysis", queries=5, tags=["corporate_action"],
   reasons=["WARN Act filings provide some advance notice"])

_r(r"\b(stock|share\s+price|market\s+cap)\b.{0,30}\b(above|below|reach|hit|\$)\b",
   "CORPORATE", "stock_price",
   researchability=55, sources=["Yahoo Finance", "Bloomberg"],
   strategy="market_data", queries=4, tags=["volatile", "price_target"],
   reasons=["Stock prices are public but volatile"])

# ── LEGAL ────────────────────────────────────────────────────────────

_r(r"\b(supreme\s+court|scotus|circuit\s+court|federal\s+court|court\s+rul(e|ing))\b",
   "LEGAL", "court_cases",
   researchability=80, sources=["SCOTUS Blog", "court filings", "Reuters"],
   strategy="news_analysis", queries=6, tags=["legal_proceeding"],
   reasons=["Court calendars and oral argument dates are public"])

_r(r"\b(indict(ment|ed)?|convict(ed|ion)?|guilty|acquit|sentenc(e|ing)|trial\s+verdict)\b",
   "LEGAL", "criminal",
   researchability=75, sources=["PACER", "Reuters", "AP News"],
   strategy="news_analysis", queries=5, tags=["legal_proceeding"],
   reasons=["Trial schedules and filings are public record"])

_r(r"\b(antitrust|ftc|doj\s+(su|investigat)|regulatory\s+(action|fine|probe)|fda\s+(approv|reject))\b",
   "LEGAL", "regulatory",
   researchability=78, sources=["FTC.gov", "FDA.gov", "Reuters"],
   strategy="official_data", queries=6, tags=["regulatory"],
   reasons=["Regulatory filings and decisions are public"])

# ── SCIENCE / TECH ───────────────────────────────────────────────────

_r(r"\b(fda|drug\s+approval|clinical\s+trial|phase\s+[123]|pdufa|pharma)\b",
   "SCIENCE", "pharma",
   researchability=82, sources=["FDA.gov", "ClinicalTrials.gov", "STAT News"],
   strategy="official_data", queries=6, tags=["scheduled_event", "regulatory"],
   reasons=["PDUFA dates are scheduled in advance",
            "Clinical trial data on ClinicalTrials.gov"])

_r(r"\b(spacex|nasa|rocket|launch|satellite|orbit|mars|moon\s+land|artemis|starship)\b",
   "SCIENCE", "space",
   researchability=80, sources=["NASA.gov", "SpaceX", "Space.com"],
   strategy="news_analysis", queries=5, tags=["scheduled_event"],
   reasons=["Launch windows are publicly scheduled"])

_r(r"\b(ai\s+(model|regulation|safety|company)|openai|gpt.?[45]|anthropic|google\s+(ai|gemini|deepmind)|artificial\s+intelligence)\b",
   "TECH", "ai",
   researchability=60, sources=["TechCrunch", "The Verge", "ArXiv"],
   strategy="news_analysis", queries=5, tags=["fast_moving"],
   reasons=["AI news moves fast — hard to predict specifics"])

_r(r"\b(apple|google|microsoft|meta|amazon|tesla)\b.{0,40}\b(launch|announc|releas|product|feature)\b",
   "TECH", "product_launch",
   researchability=65, sources=["The Verge", "TechCrunch", "company blogs"],
   strategy="news_analysis", queries=5, tags=["corporate_action"],
   reasons=["Tech launch rumors are common but unreliable"])

# ── SPORTS ───────────────────────────────────────────────────────────

_r(r"\b(super\s+bowl|nfl|nba\s+final|world\s+series|mlb|nhl|stanley\s+cup|world\s+cup|premier\s+league|champions\s+league)\b",
   "SPORTS", "major_leagues",
   researchability=50, sources=["ESPN", "FiveThirtyEight Sports"],
   strategy="sports_odds", queries=3, tags=["odds_available", "unpredictable"],
   reasons=["Sports odds readily available but highly unpredictable",
            "Our model has no edge over dedicated sportsbooks"])

_r(r"\b(ufc|mma|boxing|fight|bout|knockout)\b",
   "SPORTS", "combat",
   researchability=40, sources=["ESPN", "Sherdog"],
   strategy="sports_odds", queries=2, tags=["odds_available", "unpredictable"],
   reasons=["Combat sports are extremely unpredictable"])

_r(r"\b(formula\s*1|f1|nascar|indy\s*500|motogp|race\s+winner)\b",
   "SPORTS", "motorsport",
   researchability=42, sources=["formula1.com", "ESPN"],
   strategy="sports_odds", queries=2, tags=["odds_available", "unpredictable"],
   reasons=["Motorsport outcomes highly dependent on race-day conditions"])

# General sports fallback
_r(r"\b(score|win\s+game|playoff|championship|mvp|draft\s+pick|season\s+record|sport)\b",
   "SPORTS", "general",
   researchability=40, sources=["ESPN"],
   strategy="sports_odds", queries=2, tags=["odds_available", "unpredictable"],
   reasons=["Sports outcomes are hard to predict without domain expertise"])

# ── WEATHER ──────────────────────────────────────────────────────────

_r(r"\b(hurricane|tropical\s+storm|typhoon|cyclone|category\s+[1-5])\b",
   "WEATHER", "severe_weather",
   researchability=70, sources=["NOAA", "NHC", "Weather.gov"],
   strategy="official_data", queries=5, tags=["time_sensitive", "nowcast"],
   reasons=["NOAA provides excellent tracking data for active storms"])

_r(r"\b(temperature|heat\s+(wave|record)|cold\s+(snap|record)|snow|rainfall|drought|flood)\b",
   "WEATHER", "forecast",
   researchability=55, sources=["NOAA", "Weather.gov", "AccuWeather"],
   strategy="official_data", queries=4, tags=["nowcast"],
   reasons=["Weather forecasts degrade beyond 7-10 days"])

_r(r"\b(earthquake|wildfire|tornado|volcan|tsunami)\b",
   "WEATHER", "natural_disaster",
   researchability=35, sources=["USGS", "NOAA"],
   strategy="official_data", queries=3, tags=["unpredictable"],
   reasons=["Natural disasters are inherently unpredictable"])

# ── GEOPOLITICS ──────────────────────────────────────────────────────

_r(r"\b(war|invasion|military\s+(action|strike)|conflict|ceasefire|peace\s+(deal|talk))\b",
   "GEOPOLITICS", "conflict",
   researchability=62, sources=["Reuters", "AP News", "BBC"],
   strategy="news_analysis", queries=5, tags=["political", "fast_moving"],
   reasons=["Conflict situations are closely covered by wire services"])

_r(r"\b(sanctions?|embargo|diplomacy|treaty|summit|nato|un\s+vote|g[78]|g20)\b",
   "GEOPOLITICS", "diplomacy",
   researchability=65, sources=["Reuters", "AP News", "Foreign Affairs"],
   strategy="news_analysis", queries=5, tags=["political"],
   reasons=["Diplomatic events have press coverage and schedules"])

# ── SOCIAL MEDIA / CULTURE ───────────────────────────────────────────

_r(r"\b(tweet|x\.com|twitter|elon\s+musk\s+(tweet|post|say)|truth\s+social)\b",
   "SOCIAL_MEDIA", "social_posts",
   researchability=15, sources=[],
   strategy="skip", queries=2, tags=["unpredictable", "noise"],
   reasons=["Individual social media behavior is nearly impossible to predict"])

_r(r"\b(follow(er|ing)\s+count|subscri(be|ber)|like\s+count|view\s+count|viral|tiktok\s+trend)\b",
   "SOCIAL_MEDIA", "metrics",
   researchability=10, sources=[],
   strategy="skip", queries=2, tags=["unpredictable", "noise"],
   reasons=["Social media metrics are essentially random noise"])

_r(r"\b(streamer|youtuber|twitch|influencer|content\s+creator|podcast(er)?)\b",
   "SOCIAL_MEDIA", "influencer",
   researchability=12, sources=[],
   strategy="skip", queries=2, tags=["unpredictable", "noise"],
   reasons=["Influencer behavior is unpredictable"])

_r(r"\b(celebrity|dating|breakup|engaged|married|baby\s+name|divorce)\b",
   "CULTURE", "celebrity",
   researchability=10, sources=[],
   strategy="skip", queries=2, tags=["unpredictable", "gossip"],
   reasons=["Celebrity gossip is impossible to research reliably"])

_r(r"\b(oscar|emmy|grammy|golden\s+globe|award\s+show|box\s+office|movie\s+gross|album\s+sale)\b",
   "CULTURE", "entertainment",
   researchability=55, sources=["Box Office Mojo", "Variety", "Hollywood Reporter"],
   strategy="news_analysis", queries=4, tags=["entertainment"],
   reasons=["Award shows have nomination data; box office has tracking data"])

_r(r"\b(meme\s+coin|meme\s+stock|dog\s+race|eating\s+contest|hot\s+dog|challenge|stunt|prank)\b",
   "CULTURE", "novelty",
   researchability=8, sources=[],
   strategy="skip", queries=2, tags=["unpredictable", "noise", "novelty"],
   reasons=["Novelty events are essentially random"])


# ═══════════════════════════════════════════════════════════════
#  CLASSIFIER ENGINE
# ═══════════════════════════════════════════════════════════════

# Categories that should NOT be researched at all
_SKIP_CATEGORIES: set[str] = {"SOCIAL_MEDIA"}

# Categories where we have no information edge
_LOW_EDGE_CATEGORIES: set[str] = {"SPORTS"}


def classify_market(question: str, description: str = "") -> MarketClassification:
    """Classify a market question into category/subcategory with
    researchability scoring.

    Args:
        question: The market question text.
        description: Optional market description for additional context.

    Returns:
        MarketClassification with full analysis.
    """
    text = f"{question} {description}".strip()

    for pattern, category, subcategory, config in _RULES:
        if pattern.search(text):
            reasons = config.get("reasons", [])
            sources = config.get("sources", [])
            strategy = config.get("strategy", "news_analysis")
            queries = config.get("queries", 4)
            researchability = config.get("researchability", 50)
            tags = list(config.get("tags", []))

            # Determine worth_researching from researchability & category
            worth = researchability >= 25 and category not in _SKIP_CATEGORIES

            # Adjust confidence based on where match was found
            conf = 0.85 if pattern.search(question) else 0.65

            return MarketClassification(
                category=category,
                subcategory=subcategory,
                researchability=researchability,
                researchability_reasons=list(reasons),
                primary_sources=list(sources),
                search_strategy=strategy,
                recommended_queries=queries,
                worth_researching=worth,
                confidence=conf,
                tags=tags,
            )

    # ── Fallback: UNKNOWN ────────────────────────────────────────────
    return MarketClassification(
        category="UNKNOWN",
        subcategory="unknown",
        researchability=30,
        researchability_reasons=["No matching classification rule"],
        primary_sources=[],
        search_strategy="news_analysis",
        recommended_queries=3,
        worth_researching=False,
        confidence=0.2,
        tags=["unclassified"],
    )


def classify_and_log(market: Any) -> MarketClassification:
    """Convenience wrapper — classify a GammaMarket and log the result."""
    question = getattr(market, "question", "")
    description = getattr(market, "description", "")
    result = classify_market(question, description)

    log.info(
        "classifier.result",
        market_id=getattr(market, "id", "?"),
        category=result.category,
        subcategory=result.subcategory,
        researchability=result.researchability,
        worth=result.worth_researching,
        queries=result.recommended_queries,
    )
    return result


# ═══════════════════════════════════════════════════════════════
#  BATCH STATISTICS
# ═══════════════════════════════════════════════════════════════


def classify_batch(markets: list[Any]) -> Dict[str, int]:
    """Classify a list of markets and return category breakdown counts."""
    breakdown: Dict[str, int] = {}
    for m in markets:
        c = classify_market(
            getattr(m, "question", ""),
            getattr(m, "description", ""),
        )
        breakdown[c.category] = breakdown.get(c.category, 0) + 1
    return breakdown

"""Tests for src.engine.market_classifier — advanced market classification."""

from __future__ import annotations

import pytest

from src.engine.market_classifier import (
    MarketClassification,
    classify_batch,
    classify_market,
    classify_and_log,
)


# ═══════════════════════════════════════════════════════════════
#  DATACLASS TESTS
# ═══════════════════════════════════════════════════════════════


class TestMarketClassificationDataclass:
    """MarketClassification to_dict / from_dict round-trip."""

    def test_to_dict_contains_all_fields(self):
        mc = MarketClassification(
            category="MACRO", subcategory="fed_rates",
            researchability=92, researchability_reasons=["Has schedule"],
            primary_sources=["fed.gov"], search_strategy="official_data",
            recommended_queries=8, worth_researching=True,
            confidence=0.9, tags=["scheduled_event"],
        )
        d = mc.to_dict()
        assert d["category"] == "MACRO"
        assert d["subcategory"] == "fed_rates"
        assert d["researchability"] == 92
        assert d["recommended_queries"] == 8
        assert d["worth_researching"] is True
        assert "scheduled_event" in d["tags"]

    def test_from_dict_round_trip(self):
        mc = MarketClassification(
            category="ELECTION", subcategory="presidential",
            researchability=88,
        )
        d = mc.to_dict()
        mc2 = MarketClassification.from_dict(d)
        assert mc2.category == "ELECTION"
        assert mc2.subcategory == "presidential"
        assert mc2.researchability == 88

    def test_from_dict_none_input(self):
        mc = MarketClassification.from_dict(None)
        assert mc.category == "UNKNOWN"
        assert mc.worth_researching is False

    def test_from_dict_empty_dict(self):
        mc = MarketClassification.from_dict({})
        assert mc.category == "UNKNOWN"

    def test_from_dict_partial(self):
        mc = MarketClassification.from_dict({"category": "SPORTS"})
        assert mc.category == "SPORTS"
        assert mc.subcategory == "unknown"


# ═══════════════════════════════════════════════════════════════
#  MACRO CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


class TestMacroClassification:

    def test_fed_rate_cut(self):
        c = classify_market("Will the Fed cut interest rates in June 2025?")
        assert c.category == "MACRO"
        assert c.subcategory == "fed_rates"
        assert c.researchability >= 85

    def test_fomc(self):
        c = classify_market("Will FOMC hold rates at the December meeting?")
        assert c.category == "MACRO"
        assert c.subcategory == "fed_rates"
        assert c.worth_researching is True

    def test_cpi_inflation(self):
        c = classify_market("Will CPI come in above 3.5% for January 2025?")
        assert c.category == "MACRO"
        assert c.subcategory == "inflation"
        assert c.researchability >= 85

    def test_inflation_general(self):
        c = classify_market("Will inflation exceed 4% by end of year?")
        assert c.category == "MACRO"
        assert c.subcategory == "inflation"

    def test_gdp_growth(self):
        c = classify_market("Will Q4 GDP growth exceed 3%?")
        assert c.category == "MACRO"
        assert c.subcategory == "gdp"

    def test_unemployment(self):
        c = classify_market("Will unemployment rise above 5% by July?")
        assert c.category == "MACRO"
        assert c.subcategory == "employment"

    def test_nonfarm_payrolls(self):
        c = classify_market("Will nonfarm payrolls exceed 200K?")
        assert c.category == "MACRO"
        assert c.subcategory == "employment"

    def test_tariff(self):
        c = classify_market("Will the US impose new tariffs on China?")
        assert c.category == "MACRO"
        assert c.subcategory == "trade"

    def test_recession(self):
        c = classify_market("Will the US enter a recession in 2025?")
        assert c.category == "MACRO"
        assert c.subcategory == "recession"

    def test_treasury_yield(self):
        c = classify_market("Will the 10-year treasury yield exceed 5%?")
        assert c.category == "MACRO"
        assert c.subcategory == "bonds"

    def test_macro_has_high_queries(self):
        c = classify_market("Will the Federal Reserve announce a rate hike?")
        assert c.recommended_queries >= 6

    def test_macro_has_scheduled_event_tag(self):
        c = classify_market("Will CPI exceed expectations for March?")
        assert "scheduled_event" in c.tags or "data_release" in c.tags


# ═══════════════════════════════════════════════════════════════
#  ELECTION CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


class TestElectionClassification:

    def test_presidential(self):
        c = classify_market("Will Trump win the 2024 presidential election?")
        assert c.category == "ELECTION"
        assert c.subcategory == "presidential"
        assert c.researchability >= 80

    def test_senate(self):
        c = classify_market("Will Democrats win the Senate in 2024?")
        assert c.category == "ELECTION"
        assert c.subcategory == "congressional"

    def test_governor(self):
        c = classify_market("Will the governor of Texas sign the bill?")
        assert c.category == "ELECTION"
        assert c.subcategory == "state_local"

    def test_cabinet_appointment(self):
        c = classify_market("Will the nominee for Secretary of State be confirmed?")
        assert c.category == "ELECTION"
        assert c.subcategory == "appointments"

    def test_legislation(self):
        c = classify_market("Will the immigration bill pass the House?")
        assert c.category == "ELECTION"
        assert c.subcategory == "legislation"

    def test_general_election(self):
        c = classify_market("Will voter turnout exceed 70% in the election?")
        assert c.category == "ELECTION"
        assert c.worth_researching is True


# ═══════════════════════════════════════════════════════════════
#  CRYPTO CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


class TestCryptoClassification:

    def test_btc_price(self):
        c = classify_market("Will Bitcoin hit $100K by June 2025?")
        assert c.category == "CRYPTO"
        assert c.subcategory == "btc_price"
        assert c.researchability >= 50

    def test_eth_price(self):
        c = classify_market("Will Ethereum price reach $5000?")
        assert c.category == "CRYPTO"
        assert c.subcategory == "eth_price"

    def test_altcoin(self):
        c = classify_market("Will Dogecoin price hit $1?")
        assert c.category == "CRYPTO"
        assert c.subcategory == "altcoin_price"
        assert c.researchability < 60

    def test_crypto_regulation(self):
        c = classify_market("Will the SEC approve a spot Bitcoin ETF?")
        assert c.category == "CRYPTO"
        assert c.subcategory == "crypto_regulation"
        assert c.researchability >= 70

    def test_crypto_event(self):
        c = classify_market("Will the Bitcoin halving happen before May?")
        assert c.category == "CRYPTO"
        assert c.subcategory == "crypto_events"

    def test_crypto_low_queries(self):
        c = classify_market("Will Solana price pump to $500?")
        assert c.recommended_queries <= 4


# ═══════════════════════════════════════════════════════════════
#  CORPORATE CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


class TestCorporateClassification:

    def test_earnings(self):
        c = classify_market("Will Apple beat earnings estimates in Q2?")
        assert c.category == "CORPORATE"
        assert c.subcategory == "earnings"
        assert c.researchability >= 80

    def test_ipo(self):
        c = classify_market("Will Stripe IPO before December 2025?")
        assert c.category == "CORPORATE"
        assert c.subcategory == "ipo"

    def test_merger(self):
        c = classify_market("Will the Microsoft-Activision merger close?")
        assert c.category == "CORPORATE"
        assert c.subcategory == "mna"

    def test_layoffs(self):
        c = classify_market("Will Google announce another layoff round?")
        assert c.category == "CORPORATE"
        assert c.subcategory == "layoffs"


# ═══════════════════════════════════════════════════════════════
#  LEGAL CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


class TestLegalClassification:

    def test_supreme_court(self):
        c = classify_market("Will the Supreme Court rule on abortion?")
        assert c.category == "LEGAL"
        assert c.subcategory == "court_cases"
        assert c.researchability >= 70

    def test_indictment(self):
        c = classify_market("Will there be an indictment by March?")
        assert c.category == "LEGAL"
        assert c.subcategory == "criminal"

    def test_antitrust(self):
        c = classify_market("Will the FTC block the deal?")
        assert c.category == "LEGAL"
        assert c.subcategory == "regulatory"


# ═══════════════════════════════════════════════════════════════
#  SCIENCE / TECH CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


class TestScienceTechClassification:

    def test_fda_approval(self):
        c = classify_market("Will FDA approve the new drug?")
        assert c.category == "SCIENCE"
        assert c.subcategory == "pharma"
        assert c.researchability >= 75

    def test_spacex_launch(self):
        c = classify_market("Will SpaceX Starship reach orbit by 2025?")
        assert c.category == "SCIENCE"
        assert c.subcategory == "space"

    def test_ai_company(self):
        c = classify_market("Will OpenAI release GPT-5 this year?")
        assert c.category == "TECH"
        assert c.subcategory == "ai"


# ═══════════════════════════════════════════════════════════════
#  SPORTS CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


class TestSportsClassification:

    def test_super_bowl(self):
        c = classify_market("Will the Chiefs win the Super Bowl?")
        assert c.category == "SPORTS"
        assert c.subcategory == "major_leagues"
        assert c.researchability <= 55

    def test_ufc(self):
        c = classify_market("Will the UFC champion defend the title?")
        assert c.category == "SPORTS"
        assert c.subcategory == "combat"

    def test_f1(self):
        c = classify_market("Will Verstappen win the F1 championship?")
        assert c.category == "SPORTS"
        assert c.subcategory == "motorsport"

    def test_sports_low_queries(self):
        c = classify_market("Will the NBA finals go to game 7?")
        assert c.recommended_queries <= 4


# ═══════════════════════════════════════════════════════════════
#  WEATHER CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


class TestWeatherClassification:

    def test_hurricane(self):
        c = classify_market("Will a category 5 hurricane hit Florida?")
        assert c.category == "WEATHER"
        assert c.subcategory == "severe_weather"

    def test_temperature(self):
        c = classify_market("Will the temperature exceed 110°F in Phoenix?")
        assert c.category == "WEATHER"
        assert c.subcategory == "forecast"

    def test_earthquake(self):
        c = classify_market("Will an earthquake hit California this year?")
        assert c.category == "WEATHER"
        assert c.subcategory == "natural_disaster"
        assert c.researchability < 50


# ═══════════════════════════════════════════════════════════════
#  GEOPOLITICS CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


class TestGeopoliticsClassification:

    def test_conflict(self):
        c = classify_market("Will there be a ceasefire in Gaza?")
        assert c.category == "GEOPOLITICS"
        assert c.subcategory == "conflict"

    def test_sanctions(self):
        c = classify_market("Will the US impose new sanctions on Russia?")
        assert c.category == "GEOPOLITICS"
        assert c.subcategory == "diplomacy"


# ═══════════════════════════════════════════════════════════════
#  SOCIAL MEDIA / CULTURE — SHOULD BE BLOCKED
# ═══════════════════════════════════════════════════════════════


class TestSocialMediaCulture:

    def test_twitter_post(self):
        c = classify_market("Will Elon Musk tweet about Dogecoin today?")
        assert c.category == "SOCIAL_MEDIA"
        assert c.worth_researching is False
        assert c.researchability < 25

    def test_follower_count(self):
        c = classify_market("Will MrBeast hit 200M subscriber count?")
        assert c.category == "SOCIAL_MEDIA"
        assert c.worth_researching is False

    def test_streamer(self):
        c = classify_market("Will this Twitch streamer break a record?")
        assert c.category == "SOCIAL_MEDIA"
        assert c.worth_researching is False

    def test_celebrity(self):
        c = classify_market("Will the celebrity couple announce a breakup?")
        assert c.category == "CULTURE"
        assert c.worth_researching is False

    def test_meme_coin(self):
        c = classify_market("Will this meme coin pump 10x?")
        assert c.category == "CULTURE"
        assert c.subcategory == "novelty"
        assert c.researchability < 15

    def test_entertainment_awards(self):
        c = classify_market("Will this movie win the Oscar for best picture?")
        assert c.category == "CULTURE"
        assert c.subcategory == "entertainment"
        assert c.worth_researching is True  # awards are researchable


# ═══════════════════════════════════════════════════════════════
#  UNKNOWN / FALLBACK
# ═══════════════════════════════════════════════════════════════


class TestUnknownFallback:

    def test_unknown_question(self):
        c = classify_market("Will something random happen?")
        assert c.category == "UNKNOWN"
        assert c.subcategory == "unknown"
        assert c.worth_researching is False
        assert c.confidence < 0.5

    def test_empty_question(self):
        c = classify_market("")
        assert c.category == "UNKNOWN"
        assert c.worth_researching is False

    def test_unknown_low_queries(self):
        c = classify_market("Will the thing do the stuff?")
        assert c.recommended_queries <= 4


# ═══════════════════════════════════════════════════════════════
#  RESEARCHABILITY SCORE RANGES
# ═══════════════════════════════════════════════════════════════


class TestResearchabilityRanges:
    """Verify the relative ordering of researchability scores."""

    def test_macro_higher_than_sports(self):
        macro = classify_market("Will the Fed cut rates?")
        sports = classify_market("Will the Chiefs win the Super Bowl?")
        assert macro.researchability > sports.researchability

    def test_election_higher_than_social_media(self):
        election = classify_market("Will Biden win the election?")
        social = classify_market("Will this TikTok trend go viral?")
        assert election.researchability > social.researchability

    def test_corporate_higher_than_novelty(self):
        corp = classify_market("Will Apple beat earnings estimates?")
        novelty = classify_market("Who wins the hot dog eating contest?")
        assert corp.researchability > novelty.researchability

    def test_scheduled_events_highest(self):
        fed = classify_market("Will the FOMC raise rates?")
        assert fed.researchability >= 85


# ═══════════════════════════════════════════════════════════════
#  CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════


class TestConvenienceFunctions:

    def test_classify_and_log(self):
        class FakeMarket:
            id = "m1"
            question = "Will the Fed cut rates?"
            description = ""
        c = classify_and_log(FakeMarket())
        assert c.category == "MACRO"
        assert c.subcategory == "fed_rates"

    def test_classify_batch(self):
        class FM:
            def __init__(self, q):
                self.question = q
                self.description = ""
        markets = [
            FM("Will the Fed cut rates?"),
            FM("Will Bitcoin hit 100K?"),
            FM("Will the Chiefs win the Super Bowl?"),
            FM("Something random"),
        ]
        breakdown = classify_batch(markets)
        assert "MACRO" in breakdown
        assert "CRYPTO" in breakdown
        assert "SPORTS" in breakdown
        assert sum(breakdown.values()) == 4

    def test_classify_batch_empty(self):
        breakdown = classify_batch([])
        assert breakdown == {}


# ═══════════════════════════════════════════════════════════════
#  DESCRIPTION FALLBACK
# ═══════════════════════════════════════════════════════════════


class TestDescriptionFallback:
    """If question doesn't match, description text can trigger rules."""

    def test_description_triggers_match(self):
        c = classify_market(
            "Will this happen?",
            description="This market tracks the Federal Reserve interest rate decision."
        )
        assert c.category == "MACRO"

    def test_question_takes_priority_for_confidence(self):
        c = classify_market(
            "Will the Fed cut rates?",
            description="Some extra context",
        )
        assert c.confidence >= 0.8

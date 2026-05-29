"""Tests for market parsing from Gamma API responses."""

from __future__ import annotations

from src.connectors.polymarket_gamma import parse_market, classify_market_type


class TestParseMarket:
    """Test parsing raw Gamma API responses into GammaMarket objects."""

    def test_parse_basic_market(self) -> None:
        raw = {
            "id": "12345",
            "condition_id": "cond_abc",
            "question": "Will CPI exceed 3% in January 2026?",
            "description": "Consumer Price Index year-over-year",
            "category": "Economics",
            "active": True,
            "closed": False,
            "volume": 150000,
            "liquidity": 25000,
            "resolution_source": "Bureau of Labor Statistics (bls.gov)",
            "slug": "will-cpi-exceed-3-jan-2026",
            "tokens": [
                {"token_id": "tok_yes", "outcome": "Yes", "price": 0.65},
                {"token_id": "tok_no", "outcome": "No", "price": 0.35},
            ],
            "end_date_iso": "2026-02-15T00:00:00Z",
        }

        market = parse_market(raw)

        assert market.id == "12345"
        assert market.condition_id == "cond_abc"
        assert market.question == "Will CPI exceed 3% in January 2026?"
        assert market.category == "Economics"
        assert market.active is True
        assert market.closed is False
        assert market.volume == 150000
        assert market.liquidity == 25000
        assert market.resolution_source == "Bureau of Labor Statistics (bls.gov)"
        assert market.slug == "will-cpi-exceed-3-jan-2026"
        assert len(market.tokens) == 2
        assert market.tokens[0].outcome == "Yes"
        assert market.tokens[0].price == 0.65
        assert market.tokens[1].outcome == "No"
        assert market.tokens[1].price == 0.35
        assert market.end_date is not None
        assert market.market_type == "MACRO"  # CPI → MACRO

    def test_parse_market_best_bid(self) -> None:
        raw = {
            "id": "999",
            "question": "Test market",
            "tokens": [
                {"token_id": "t1", "outcome": "Yes", "price": 0.72},
                {"token_id": "t2", "outcome": "No", "price": 0.28},
            ],
        }
        market = parse_market(raw)
        assert market.best_bid == 0.72

    def test_parse_market_no_yes_token(self) -> None:
        raw = {
            "id": "888",
            "question": "Multi-option market",
            "tokens": [
                {"token_id": "t1", "outcome": "Option A", "price": 0.40},
                {"token_id": "t2", "outcome": "Option B", "price": 0.60},
            ],
        }
        market = parse_market(raw)
        assert market.best_bid == 0.40  # Falls back to first token's price

    def test_parse_market_string_outcomes(self) -> None:
        """Handle the case where outcomes are just strings, not dicts."""
        raw = {
            "id": "777",
            "question": "Who wins the election?",
            "outcomes": ["Candidate A", "Candidate B"],
        }
        market = parse_market(raw)
        assert len(market.tokens) == 2
        assert market.tokens[0].outcome == "Candidate A"
        assert market.tokens[0].price == 0.0

    def test_parse_market_gamma_json_string_format(self) -> None:
        """Handle the real Gamma API list endpoint format where fields are JSON strings."""
        raw = {
            "id": "12345",
            "question": "Will XYZ happen?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.65", "0.35"]',
            "clobTokenIds": '["abc123", "def456"]',
            "conditionId": "0xabc",
            "volume": "5000",
            "liquidity": "2000",
            "endDate": "2026-06-01T00:00:00Z",
        }
        market = parse_market(raw)
        assert len(market.tokens) == 2
        assert market.tokens[0].outcome == "Yes"
        assert market.tokens[0].price == 0.65
        assert market.tokens[0].token_id == "abc123"
        assert market.tokens[1].outcome == "No"
        assert market.tokens[1].price == 0.35
        assert market.tokens[1].token_id == "def456"
        assert market.best_bid == 0.65
        assert market.condition_id == "0xabc"

    def test_parse_market_missing_fields(self) -> None:
        """Gracefully handle minimal data."""
        raw = {"id": "666"}
        market = parse_market(raw)
        assert market.id == "666"
        assert market.question == ""
        assert market.volume == 0
        assert len(market.tokens) == 0
        assert market.end_date is None

    def test_parse_market_spread(self) -> None:
        raw = {
            "id": "555",
            "question": "Test",
            "tokens": [
                {"token_id": "t1", "outcome": "Yes", "price": 0.60},
                {"token_id": "t2", "outcome": "No", "price": 0.38},
            ],
        }
        market = parse_market(raw)
        # spread = |1.0 - (0.60 + 0.38)| = 0.02
        assert abs(market.spread - 0.02) < 0.001

    def test_parse_market_end_date_formats(self) -> None:
        """Handle various date formats."""
        # ISO with Z
        raw = {"id": "1", "end_date_iso": "2026-03-01T12:00:00Z"}
        m1 = parse_market(raw)
        assert m1.end_date is not None

        # ISO without Z
        raw2 = {"id": "2", "endDate": "2026-03-01T12:00:00+00:00"}
        m2 = parse_market(raw2)
        assert m2.end_date is not None

        # Invalid date
        raw3 = {"id": "3", "end_date": "not-a-date"}
        m3 = parse_market(raw3)
        assert m3.end_date is None

    def test_has_clear_resolution(self) -> None:
        raw = {
            "id": "444",
            "question": "Test",
            "resolution_source": "Bureau of Labor Statistics",
        }
        market = parse_market(raw)
        assert market.has_clear_resolution is True

        raw2 = {"id": "445", "question": "Test"}
        market2 = parse_market(raw2)
        assert market2.has_clear_resolution is False

    def test_alternative_field_names(self) -> None:
        """Test parsing with camelCase field names (API variants)."""
        raw = {
            "conditionId": "cond_xyz",
            "title": "Alternative title field",
            "tag": "Politics",
            "volumeNum": 99000,
            "liquidityNum": 5000,
            "resolutionSource": "FEC.gov",
        }
        market = parse_market(raw)
        assert market.condition_id == "cond_xyz"
        assert market.question == "Alternative title field"
        assert market.category == "Politics"
        assert market.volume == 99000
        assert market.liquidity == 5000
        assert market.resolution_source == "FEC.gov"


class TestClassifyMarketType:
    """Test market type classification."""

    def test_macro_market(self) -> None:
        assert classify_market_type("Will CPI exceed 3% in January?") == "MACRO"
        assert classify_market_type("Will the Fed cut interest rates?") == "MACRO"
        assert classify_market_type("Unemployment rate above 4%?") == "MACRO"

    def test_election_market(self) -> None:
        assert classify_market_type("Who will win the 2026 Senate election?") == "ELECTION"
        assert classify_market_type("Will the president be re-elected?") == "ELECTION"

    def test_corporate_market(self) -> None:
        assert classify_market_type("Will the SEC approve the merger?") == "CORPORATE"
        assert classify_market_type("Company XYZ IPO before March?") == "CORPORATE"

    def test_weather_market(self) -> None:
        assert classify_market_type("Will a hurricane make landfall in Florida?") == "WEATHER"

    def test_sports_market(self) -> None:
        assert classify_market_type("Who will win the Super Bowl?") == "SPORTS"

    def test_unknown_market(self) -> None:
        assert classify_market_type("Something completely unrelated") == "UNKNOWN"


class TestCreatedAtParsing:
    """Test created_at / age_hours parsing from Gamma API raw data."""

    def test_parse_startDate(self) -> None:
        raw = {
            "id": "100",
            "question": "Test",
            "startDate": "2026-02-12T01:00:00Z",
        }
        market = parse_market(raw)
        assert market.created_at is not None
        assert market.created_at.year == 2026

    def test_parse_acceptingOrdersTimestamp(self) -> None:
        raw = {
            "id": "101",
            "question": "Test",
            "acceptingOrdersTimestamp": "2026-02-11T12:00:00Z",
        }
        market = parse_market(raw)
        assert market.created_at is not None

    def test_parse_createdAt(self) -> None:
        raw = {
            "id": "102",
            "question": "Test",
            "createdAt": "2026-02-10T08:00:00.123456Z",
        }
        market = parse_market(raw)
        assert market.created_at is not None

    def test_startDate_preferred_over_createdAt(self) -> None:
        """startDate should be preferred because it reflects trading start."""
        raw = {
            "id": "103",
            "question": "Test",
            "startDate": "2026-02-12T06:00:00Z",
            "createdAt": "2026-02-11T01:00:00Z",
        }
        market = parse_market(raw)
        assert market.created_at is not None
        assert market.created_at.day == 12  # startDate, not createdAt

    def test_no_created_at(self) -> None:
        raw = {"id": "104", "question": "Test"}
        market = parse_market(raw)
        assert market.created_at is None

    def test_age_hours(self) -> None:
        import datetime as dt
        raw = {
            "id": "105",
            "question": "Test",
            "startDate": (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
            ).isoformat().replace("+00:00", "Z"),
        }
        market = parse_market(raw)
        assert market.age_hours is not None
        assert 4.9 < market.age_hours < 5.5

    def test_age_hours_none_when_no_date(self) -> None:
        raw = {"id": "106", "question": "Test"}
        market = parse_market(raw)
        assert market.age_hours is None

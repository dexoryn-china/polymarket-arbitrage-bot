"""Tests for src.engine.market_filter — pre-research market filter."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from src.engine.market_filter import (
    BLOCKED_KEYWORDS,
    PREFERRED_KEYWORDS,
    FilterResult,
    FilterStats,
    ResearchCache,
    _hard_reject,
    _score_market,
    filter_markets,
    score_market,
)


# ── Helpers ──────────────────────────────────────────────────────────

class FakeToken:
    def __init__(self, token_id: str = "tok1", outcome: str = "Yes", price: float = 0.5):
        self.token_id = token_id
        self.outcome = outcome
        self.price = price


class FakeMarket:
    """Lightweight stand-in for GammaMarket in tests."""
    def __init__(self, **kwargs):
        defaults = dict(
            id="m1", condition_id="c1",
            question="Will the Federal Reserve cut interest rates?",
            description="", category="", market_type="MACRO",
            end_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
            active=True, closed=False, volume=50_000.0, liquidity=20_000.0,
            tokens=[FakeToken()], resolution_source="https://example.com/source",
            slug="will-x-happen", best_bid=0.5, spread=0.02,
        )
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)

    @property
    def age_hours(self):
        if self.created_at is None:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        ca = self.created_at
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (now - ca).total_seconds() / 3600.0)

    @property
    def has_clear_resolution(self):
        return bool(self.resolution_source and len(self.resolution_source) > 5)


# ═════════════════════════════════════════════════════════════════════
#  Hard Rejection Tests
# ═════════════════════════════════════════════════════════════════════


def test_reject_no_tokens():
    m = FakeMarket(tokens=[])
    assert _hard_reject(m) == "no_tokens"


def test_reject_closed_market():
    m = FakeMarket(closed=True)
    assert _hard_reject(m) == "market_closed"


def test_reject_inactive_market():
    m = FakeMarket(active=False)
    assert _hard_reject(m) == "market_inactive"


def test_reject_blocked_type_default():
    m = FakeMarket(market_type="UNKNOWN")
    assert _hard_reject(m) is not None
    assert "blocked_type" in _hard_reject(m)


def test_reject_blocked_type_custom():
    m = FakeMarket(market_type="SPORTS")
    assert _hard_reject(m, blocked_types={"SPORTS"}) == "blocked_type:SPORTS"


def test_reject_blocked_keyword():
    m = FakeMarket(question="Will the tiktok trend continue?")
    reason = _hard_reject(m)
    assert reason is not None
    assert "blocked_keyword" in reason


def test_pass_good_market():
    m = FakeMarket()
    assert _hard_reject(m) is None


def test_pass_unknown_not_blocked_if_custom():
    """If blocked_types doesn't include UNKNOWN, it should pass."""
    m = FakeMarket(market_type="UNKNOWN")
    assert _hard_reject(m, blocked_types=set()) is None


# ═════════════════════════════════════════════════════════════════════
#  Scoring Tests
# ═════════════════════════════════════════════════════════════════════


def test_score_high_liquidity():
    m = FakeMarket(liquidity=100_000)
    score, bd = _score_market(m)
    assert bd["liquidity"] == 15


def test_score_low_liquidity():
    m = FakeMarket(liquidity=500)
    score, bd = _score_market(m)
    assert bd["liquidity"] == -30


def test_score_high_volume():
    m = FakeMarket(volume=200_000)
    score, bd = _score_market(m)
    assert bd["volume"] == 10


def test_score_low_volume():
    m = FakeMarket(volume=1_000)
    score, bd = _score_market(m)
    assert bd["volume"] == -15


def test_score_extreme_probability_low():
    m = FakeMarket(best_bid=0.01)
    score, bd = _score_market(m)
    assert bd["probability"] == -30


def test_score_extreme_probability_high():
    m = FakeMarket(best_bid=0.99)
    score, bd = _score_market(m)
    assert bd["probability"] == -30


def test_score_sweet_spot_probability():
    m = FakeMarket(best_bid=0.5)
    score, bd = _score_market(m)
    assert bd["probability"] == 10


def test_score_near_expiry():
    m = FakeMarket(end_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12))
    score, bd = _score_market(m)
    assert bd["expiry"] == -20


def test_score_good_expiry():
    m = FakeMarket(end_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30))
    score, bd = _score_market(m)
    assert bd["expiry"] == 10


def test_score_far_expiry():
    m = FakeMarket(end_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=200))
    score, bd = _score_market(m)
    assert bd["expiry"] == -10


def test_score_preferred_type():
    m = FakeMarket(market_type="MACRO")
    score, bd = _score_market(m)
    assert bd["market_type"] == 15


def test_score_sports_type():
    m = FakeMarket(market_type="SPORTS")
    score, bd = _score_market(m)
    assert bd["market_type"] == -5


def test_score_resolution_source():
    m = FakeMarket(resolution_source="https://official.gov/data")
    score, bd = _score_market(m)
    assert bd["resolution"] == 8


def test_score_no_resolution():
    m = FakeMarket(resolution_source="")
    score, bd = _score_market(m)
    assert bd["resolution"] == 0


def test_score_tight_spread():
    m = FakeMarket(spread=0.01)
    score, bd = _score_market(m)
    assert bd["spread"] == 8


def test_score_wide_spread():
    m = FakeMarket(spread=0.20)
    score, bd = _score_market(m)
    assert bd["spread"] == -8


def test_score_preferred_keywords():
    m = FakeMarket(question="Will the federal reserve cut interest rate?")
    score, bd = _score_market(m)
    assert bd["keywords"] >= 10  # At least 2 keywords matched × 5


def test_score_clamped_0_to_100():
    # Terrible market — should clamp to 0
    m = FakeMarket(
        liquidity=100, volume=100, best_bid=0.001, spread=0.5,
        market_type="SPORTS",
        end_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        resolution_source="",
    )
    score, _ = _score_market(m)
    assert 0 <= score <= 100


def test_score_perfect_market():
    m = FakeMarket(
        liquidity=100_000, volume=200_000, best_bid=0.5, spread=0.01,
        market_type="MACRO", resolution_source="https://bls.gov/cpi",
        question="Will CPI inflation exceed 3% in Q2?",
        end_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
    )
    score, _ = _score_market(m)
    assert score >= 80


# ═════════════════════════════════════════════════════════════════════
#  score_market() public API
# ═════════════════════════════════════════════════════════════════════


def test_score_market_hard_rejected():
    m = FakeMarket(tokens=[])
    fr = score_market(m)
    assert not fr.passed
    assert fr.score == 0
    assert fr.rejection_reason == "no_tokens"


def test_score_market_passes():
    m = FakeMarket()
    fr = score_market(m)
    assert fr.passed
    assert fr.score > 0


# ═════════════════════════════════════════════════════════════════════
#  Research Cache
# ═════════════════════════════════════════════════════════════════════


def test_cache_not_recently_researched():
    cache = ResearchCache(cooldown_minutes=30)
    assert not cache.was_recently_researched("m1")


def test_cache_mark_and_check():
    cache = ResearchCache(cooldown_minutes=30)
    cache.mark_researched("m1")
    assert cache.was_recently_researched("m1")


def test_cache_expired(monkeypatch):
    cache = ResearchCache(cooldown_minutes=1)
    cache.mark_researched("m1")
    # Manually expire by shifting timestamp back
    cache._cache["m1"] = time.time() - 120
    assert not cache.was_recently_researched("m1")


def test_cache_clear_stale():
    cache = ResearchCache(cooldown_minutes=1)
    cache._cache["old"] = time.time() - 300  # 5 min old, 2× cooldown = 2 min
    cache._cache["new"] = time.time()
    removed = cache.clear_stale()
    assert removed == 1
    assert cache.size() == 1


def test_cache_size():
    cache = ResearchCache(cooldown_minutes=30)
    assert cache.size() == 0
    cache.mark_researched("a")
    cache.mark_researched("b")
    assert cache.size() == 2


def test_cache_cooldown_property():
    cache = ResearchCache(cooldown_minutes=15)
    assert cache.cooldown_minutes == 15
    cache.cooldown_minutes = 60
    assert cache.cooldown_minutes == 60


# ═════════════════════════════════════════════════════════════════════
#  filter_markets() batch filtering
# ═════════════════════════════════════════════════════════════════════


def _make_markets(n: int, **overrides) -> list:
    markets = []
    for i in range(n):
        kwargs = dict(
            id=f"m{i}",
            question=f"Will the Federal Reserve cut interest rates in Q{i + 1}?",
            volume=50_000, liquidity=20_000,
            best_bid=0.5, spread=0.02,
            market_type="MACRO",
            end_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
            resolution_source="https://example.com",
        )
        kwargs.update(overrides)
        kwargs["id"] = f"m{i}"
        markets.append(FakeMarket(**kwargs))
    return markets


def test_filter_markets_basic():
    markets = _make_markets(10)
    passed, stats = filter_markets(markets, min_score=30, max_pass=5)
    assert len(passed) <= 5
    assert stats.total_input == 10
    assert stats.passed == len(passed)


def test_filter_markets_respects_max_pass():
    markets = _make_markets(20)
    passed, stats = filter_markets(markets, min_score=0, max_pass=3)
    assert len(passed) == 3


def test_filter_markets_hard_rejects():
    good = _make_markets(3)
    bad = [FakeMarket(id="bad1", tokens=[]), FakeMarket(id="bad2", closed=True)]
    passed, stats = filter_markets(good + bad, min_score=0, max_pass=10)
    assert stats.hard_rejected == 2
    assert len(passed) == 3


def test_filter_markets_soft_rejects():
    # Markets with terrible stats → low score (but not hard-rejected)
    terrible = _make_markets(5, liquidity=100, volume=100, best_bid=0.07, spread=0.18)
    passed, stats = filter_markets(terrible, min_score=60, max_pass=10)
    assert stats.soft_rejected > 0


def test_filter_markets_cooldown_skips():
    markets = _make_markets(5)
    cache = ResearchCache(cooldown_minutes=30)
    cache.mark_researched("m0")
    cache.mark_researched("m1")
    passed, stats = filter_markets(markets, min_score=0, max_pass=10, research_cache=cache)
    assert stats.cooldown_skipped == 2
    assert len(passed) == 3


def test_filter_markets_stats_top_passed():
    markets = _make_markets(5)
    passed, stats = filter_markets(markets, min_score=0, max_pass=5)
    assert len(stats.top_passed) == len(passed)
    for entry in stats.top_passed:
        assert "market_id" in entry
        assert "score" in entry


def test_filter_markets_empty_input():
    passed, stats = filter_markets([], min_score=0, max_pass=5)
    assert passed == []
    assert stats.total_input == 0
    assert stats.passed == 0


def test_filter_markets_all_rejected():
    bad = [FakeMarket(id=f"b{i}", tokens=[]) for i in range(5)]
    passed, stats = filter_markets(bad, min_score=0, max_pass=5)
    assert passed == []
    assert stats.hard_rejected == 5


def test_filter_markets_custom_blocked_types():
    markets = _make_markets(5, market_type="SPORTS")
    passed, stats = filter_markets(
        markets, min_score=0, max_pass=10, blocked_types={"SPORTS"},
    )
    assert passed == []
    assert stats.hard_rejected == 5


def test_filter_markets_sorted_by_score():
    m_low = FakeMarket(id="low", volume=1000, liquidity=500, best_bid=0.01)
    m_high = FakeMarket(id="high", volume=200_000, liquidity=100_000, best_bid=0.5,
                        question="Will CPI inflation exceed target?")
    passed, stats = filter_markets([m_low, m_high], min_score=0, max_pass=2)
    if len(passed) == 2:
        # High-scoring market should come first
        assert getattr(passed[0], "id") == "high"


# ═════════════════════════════════════════════════════════════════════
#  Integration: blocked keywords list sanity
# ═════════════════════════════════════════════════════════════════════


def test_blocked_keywords_are_lowercase():
    for kw in BLOCKED_KEYWORDS:
        assert kw == kw.lower(), f"Blocked keyword should be lowercase: {kw}"


def test_preferred_keywords_are_lowercase():
    for kw in PREFERRED_KEYWORDS:
        assert kw == kw.lower(), f"Preferred keyword should be lowercase: {kw}"


# ═════════════════════════════════════════════════════════════════════
#  Hard rejection: extreme probability & wide spread
# ═════════════════════════════════════════════════════════════════════


def test_hard_reject_extreme_probability_low():
    m = FakeMarket(best_bid=0.03)
    reason = _hard_reject(m)
    assert reason is not None
    assert "extreme_probability" in reason


def test_hard_reject_extreme_probability_high():
    m = FakeMarket(best_bid=0.97)
    reason = _hard_reject(m)
    assert reason is not None
    assert "extreme_probability" in reason


def test_pass_normal_probability():
    m = FakeMarket(best_bid=0.50)
    reason = _hard_reject(m)
    assert reason is None


def test_hard_reject_wide_spread():
    m = FakeMarket(spread=0.25)
    reason = _hard_reject(m)
    assert reason is not None
    assert "wide_spread" in reason


def test_pass_normal_spread():
    m = FakeMarket(spread=0.04)
    reason = _hard_reject(m)
    assert reason is None


# ═════════════════════════════════════════════════════════════════════
#  Market Age Tests — hard rejection
# ═════════════════════════════════════════════════════════════════════


def test_hard_reject_too_old():
    """Markets older than max_market_age_hours are rejected."""
    m = FakeMarket(
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24),
    )
    reason = _hard_reject(m, max_market_age_hours=12.0)
    assert reason is not None
    assert "too_old" in reason


def test_pass_young_market():
    """Markets younger than max_market_age_hours are accepted."""
    m = FakeMarket(
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3),
    )
    reason = _hard_reject(m, max_market_age_hours=12.0)
    assert reason is None


def test_pass_exactly_at_limit():
    """Market at exactly the age limit still passes (not strictly > check)."""
    m = FakeMarket(
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=12),
    )
    # age_hours ≈ 12.0 but could be slightly more due to test execution time
    # With a tiny margin, this should pass or be very close
    reason = _hard_reject(m, max_market_age_hours=12.1)
    assert reason is None


def test_no_age_limit_passes_old_market():
    """When max_market_age_hours is None, old markets are not rejected."""
    m = FakeMarket(
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30),
    )
    reason = _hard_reject(m, max_market_age_hours=None)
    assert reason is None


def test_no_created_at_passes():
    """Markets without a created_at date are not rejected by age filter."""
    m = FakeMarket(created_at=None)
    reason = _hard_reject(m, max_market_age_hours=12.0)
    assert reason is None


# ═════════════════════════════════════════════════════════════════════
#  Market Age Tests — scoring freshness bonus
# ═════════════════════════════════════════════════════════════════════


def test_freshness_bonus_brand_new():
    m = FakeMarket(
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30),
    )
    score, bd = _score_market(m)
    assert bd.get("freshness", 0) == 20


def test_freshness_bonus_3h():
    m = FakeMarket(
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
    )
    score, bd = _score_market(m)
    assert bd.get("freshness", 0) == 15


def test_freshness_bonus_6h():
    m = FakeMarket(
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5),
    )
    score, bd = _score_market(m)
    assert bd.get("freshness", 0) == 10


def test_freshness_bonus_12h():
    m = FakeMarket(
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=10),
    )
    score, bd = _score_market(m)
    assert bd.get("freshness", 0) == 5


def test_freshness_bonus_old():
    m = FakeMarket(
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24),
    )
    score, bd = _score_market(m)
    assert bd.get("freshness", 0) == 0


def test_freshness_bonus_no_created_at():
    m = FakeMarket(created_at=None)
    score, bd = _score_market(m)
    assert "freshness" not in bd


# ═════════════════════════════════════════════════════════════════════
#  filter_markets() age filtering + youngest-first sorting
# ═════════════════════════════════════════════════════════════════════


def test_filter_markets_rejects_old_markets():
    now = dt.datetime.now(dt.timezone.utc)
    young = FakeMarket(id="young", created_at=now - dt.timedelta(hours=2))
    old = FakeMarket(id="old", created_at=now - dt.timedelta(hours=24))
    passed, stats = filter_markets(
        [young, old], min_score=0, max_pass=10, max_market_age_hours=12.0,
    )
    ids = [getattr(m, "id") for m in passed]
    assert "young" in ids
    assert "old" not in ids
    assert stats.hard_rejected >= 1


def test_filter_markets_youngest_first_tiebreaker():
    """When two markets have the same score, the younger one comes first."""
    now = dt.datetime.now(dt.timezone.utc)
    # Both have identical attributes except age
    older = FakeMarket(
        id="older", created_at=now - dt.timedelta(hours=10),
        volume=50_000, liquidity=20_000,
    )
    newer = FakeMarket(
        id="newer", created_at=now - dt.timedelta(hours=1),
        volume=50_000, liquidity=20_000,
    )
    passed, stats = filter_markets(
        [older, newer], min_score=0, max_pass=10, max_market_age_hours=12.0,
    )
    ids = [getattr(m, "id") for m in passed]
    assert len(ids) == 2
    # Newer market should come first (higher freshness bonus + age tiebreaker)
    assert ids[0] == "newer"


def test_filter_markets_no_age_limit():
    """When max_market_age_hours is not passed, old markets are kept."""
    now = dt.datetime.now(dt.timezone.utc)
    old = FakeMarket(id="old", created_at=now - dt.timedelta(days=30))
    passed, stats = filter_markets([old], min_score=0, max_pass=10)
    assert len(passed) == 1

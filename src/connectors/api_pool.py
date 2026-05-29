"""Multi-API Endpoint Pool with independent rate limiting.

Rotates requests across multiple API endpoints, each with its own
rate limiter.  This multiplies effective throughput:
  2 endpoints × 60 RPM = 120 RPM effective.

Selection strategies:
  - round-robin:      cycle through endpoints in order
  - least-loaded:     pick the endpoint with the most remaining quota
  - weighted-random:  probabilistic, weighted by remaining quota

Auto-health management:
  - Endpoint auto-disables after ``max_consecutive_failures`` (default 5)
  - Auto re-enables after ``recovery_cooldown_secs`` (default 120 s)

Built-in endpoints (always present):
  - data-api-primary   → https://data-api.polymarket.com   (60 RPM)
  - gamma-api-primary  → https://gamma-api.polymarket.com   (60 RPM)

Custom endpoints can be added via config.yaml → scanner.apiPool.endpoints
to multiply throughput with proxy mirrors.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Any

import httpx

from src.observability.logger import get_logger

log = get_logger(__name__)


# ── Enums ────────────────────────────────────────────────────────────

class SelectionStrategy(str, Enum):
    ROUND_ROBIN = "round-robin"
    LEAST_LOADED = "least-loaded"
    WEIGHTED_RANDOM = "weighted-random"


# ── Per-Endpoint Rate Limiter ────────────────────────────────────────

class EndpointRateLimiter:
    """Sliding-window rate limiter scoped to a single API endpoint.

    Uses a simple token-bucket approach: *rpm* tokens refill linearly
    per minute with a burst cap equal to *rpm*.
    """

    def __init__(self, rpm: int = 60):
        self.rpm = rpm
        self._tokens: float = float(rpm)
        self._last_refill: float = time.monotonic()
        self._lock = Lock()
        self._total_requests: int = 0
        self._total_waits: int = 0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            float(self.rpm),
            self._tokens + elapsed * (self.rpm / 60.0),
        )
        self._last_refill = now

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def try_acquire(self) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._total_requests += 1
                return True
            return False

    def wait_time(self) -> float:
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                return 0.0
            deficit = 1.0 - self._tokens
            return deficit / (self.rpm / 60.0)

    async def acquire(self) -> None:
        while True:
            wt = self.wait_time()
            if wt <= 0:
                if self.try_acquire():
                    return
            else:
                self._total_waits += 1
                await asyncio.sleep(min(wt, 2.0))

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "rpm": self.rpm,
            "available": round(self.available_tokens, 1),
            "total_requests": self._total_requests,
            "total_waits": self._total_waits,
        }


# ── API Endpoint ─────────────────────────────────────────────────────

@dataclass
class ApiEndpoint:
    """A single API endpoint with health tracking."""

    name: str
    base_url: str
    rpm: int = 60
    weight: float = 1.0

    # Path routing: if non-empty, only handle requests matching these prefixes
    supported_paths: list[str] = field(default_factory=list)

    # Health tracking
    healthy: bool = True
    consecutive_failures: int = 0
    disabled_at: float = 0.0
    total_successes: int = 0
    total_failures: int = 0
    last_used: float = 0.0
    last_error: str = ""

    # Rate limiter (set after init)
    limiter: EndpointRateLimiter = field(default=None, repr=False)  # type: ignore[assignment]

    # Thresholds
    max_consecutive_failures: int = 5
    recovery_cooldown_secs: float = 120.0

    def __post_init__(self) -> None:
        if self.limiter is None:
            self.limiter = EndpointRateLimiter(rpm=self.rpm)

    def supports_path(self, path: str) -> bool:
        """Check if this endpoint can handle the given path.

        If ``supported_paths`` is empty, the endpoint is universal.
        Otherwise the request path must start with one of the prefixes.
        """
        if not self.supported_paths:
            return True
        return any(path.startswith(p) for p in self.supported_paths)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.total_successes += 1
        self.last_used = time.monotonic()
        if not self.healthy:
            self.healthy = True
            log.info("api_pool.endpoint_recovered", endpoint=self.name)

    def record_failure(self, error: str = "") -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = error[:120]
        self.last_used = time.monotonic()
        if self.consecutive_failures >= self.max_consecutive_failures and self.healthy:
            self.healthy = False
            self.disabled_at = time.monotonic()
            log.warning(
                "api_pool.endpoint_disabled",
                endpoint=self.name,
                failures=self.consecutive_failures,
                error=error[:80],
            )

    def check_recovery(self) -> bool:
        """Check if a disabled endpoint should be re-enabled."""
        if self.healthy:
            return True
        elapsed = time.monotonic() - self.disabled_at
        if elapsed >= self.recovery_cooldown_secs:
            self.healthy = True
            self.consecutive_failures = 0
            log.info(
                "api_pool.endpoint_auto_recovered",
                endpoint=self.name,
                after_secs=round(elapsed, 1),
            )
            return True
        return False

    @property
    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "healthy": self.healthy,
            "supported_paths": self.supported_paths or ["*"],
            "consecutive_failures": self.consecutive_failures,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "last_error": self.last_error,
            "limiter": self.limiter.stats,
        }


# ── API Pool ─────────────────────────────────────────────────────────

# Built-in endpoint definitions
BUILTIN_ENDPOINTS = [
    ApiEndpoint(
        name="data-api-primary",
        base_url="https://data-api.polymarket.com",
        rpm=60,
        weight=1.0,
        supported_paths=["/trades", "/positions", "/activity"],
    ),
    ApiEndpoint(
        name="gamma-api-primary",
        base_url="https://gamma-api.polymarket.com",
        rpm=60,
        weight=1.0,
        supported_paths=["/markets", "/events"],
    ),
]


class ApiPool:
    """Rotates HTTP requests across multiple API endpoints.

    Each endpoint has its own independent rate limiter so effective
    throughput = sum(endpoint.rpm).

    Usage::

        pool = ApiPool()
        data = await pool.get("/trades", params={"limit": 500})
    """

    def __init__(
        self,
        strategy: str | SelectionStrategy = SelectionStrategy.LEAST_LOADED,
        custom_endpoints: list[dict] | None = None,
    ):
        if isinstance(strategy, str):
            strategy = SelectionStrategy(strategy)
        self.strategy = strategy
        self._lock = Lock()
        self._rr_index: int = 0  # round-robin counter

        # Build endpoint list
        self.endpoints: list[ApiEndpoint] = []
        for be in BUILTIN_ENDPOINTS:
            # Deep copy so each Pool instance is independent
            ep = ApiEndpoint(
                name=be.name,
                base_url=be.base_url,
                rpm=be.rpm,
                weight=be.weight,
                supported_paths=list(be.supported_paths),
            )
            self.endpoints.append(ep)

        # Add custom endpoints from config
        if custom_endpoints:
            for cfg in custom_endpoints:
                ep = ApiEndpoint(
                    name=cfg.get("name", f"custom-{len(self.endpoints)}"),
                    base_url=cfg.get("base_url", "").rstrip("/"),
                    rpm=int(cfg.get("rpm", 60)),
                    weight=float(cfg.get("weight", 1.0)),
                    supported_paths=cfg.get("supported_paths", []),
                )
                if ep.base_url:
                    self.endpoints.append(ep)
                    log.info(
                        "api_pool.custom_endpoint_added",
                        name=ep.name,
                        url=ep.base_url,
                        rpm=ep.rpm,
                    )

        self._total_requests: int = 0
        self._total_errors: int = 0
        self._created_at: float = time.monotonic()
        log.info(
            "api_pool.initialized",
            strategy=strategy.value,
            endpoints=len(self.endpoints),
            effective_rpm=self.effective_rpm,
        )

    # ── Properties ───────────────────────────────────────────────

    @property
    def effective_rpm(self) -> int:
        """Total RPM across all healthy endpoints."""
        return sum(
            ep.rpm for ep in self.endpoints
            if ep.healthy or ep.check_recovery()
        )

    @property
    def healthy_count(self) -> int:
        return sum(1 for ep in self.endpoints if ep.healthy or ep.check_recovery())

    # ── Endpoint Selection ───────────────────────────────────────

    def _get_healthy(self, path: str = "") -> list[ApiEndpoint]:
        """Return list of healthy endpoints that support the given path."""
        healthy = []
        for ep in self.endpoints:
            ep.check_recovery()
            if ep.healthy and ep.supports_path(path):
                healthy.append(ep)
        return healthy

    def _select_endpoint(self, path: str = "") -> ApiEndpoint | None:
        """Pick the next endpoint based on strategy, filtered by path support."""
        healthy = self._get_healthy(path)
        if not healthy:
            return None

        with self._lock:
            if self.strategy == SelectionStrategy.ROUND_ROBIN:
                ep = healthy[self._rr_index % len(healthy)]
                self._rr_index += 1
                return ep

            elif self.strategy == SelectionStrategy.LEAST_LOADED:
                return max(healthy, key=lambda e: e.limiter.available_tokens)

            elif self.strategy == SelectionStrategy.WEIGHTED_RANDOM:
                weights = [
                    e.weight * max(e.limiter.available_tokens, 0.1)
                    for e in healthy
                ]
                total = sum(weights)
                if total <= 0:
                    return healthy[0]
                return random.choices(healthy, weights=weights, k=1)[0]

        return healthy[0]  # fallback

    # ── HTTP Methods ─────────────────────────────────────────────

    async def get(
        self,
        path: str,
        params: dict | None = None,
        timeout: float = 30.0,
        headers: dict | None = None,
    ) -> list | dict | None:
        """Make a GET request through the pool.

        Tries up to ``len(endpoints)`` times with failover.
        Returns parsed JSON or None on total failure.
        """
        attempts = max(len(self.endpoints), 2)
        last_error = ""

        for _ in range(attempts):
            ep = self._select_endpoint(path)
            if ep is None:
                # All endpoints down — wait for recovery
                await asyncio.sleep(2.0)
                continue

            # Wait for this endpoint's rate limit
            await ep.limiter.acquire()

            url = f"{ep.base_url}{path}"
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "polymarket-bot/1.0",
                        **(headers or {}),
                    },
                ) as client:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    ep.record_success()
                    self._total_requests += 1
                    return data

            except httpx.HTTPStatusError as e:
                last_error = f"{ep.name}: HTTP {e.response.status_code}"
                ep.record_failure(last_error)
                self._total_errors += 1
                # 429 = rate limited, try another endpoint immediately
                if e.response.status_code == 429:
                    continue
                # 4xx = client error, probably same on all endpoints
                if 400 <= e.response.status_code < 500:
                    return None
                # 5xx = server error, try next endpoint
                continue

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = f"{ep.name}: {type(e).__name__}"
                ep.record_failure(last_error)
                self._total_errors += 1
                continue

            except Exception as e:
                last_error = f"{ep.name}: {str(e)[:80]}"
                ep.record_failure(last_error)
                self._total_errors += 1
                continue

        log.warning("api_pool.all_endpoints_failed", last_error=last_error)
        return None

    # ── Stats / Status ───────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        """Full pool status for dashboard display."""
        uptime = time.monotonic() - self._created_at
        return {
            "strategy": self.strategy.value,
            "endpoint_count": len(self.endpoints),
            "healthy_count": self.healthy_count,
            "effective_rpm": self.effective_rpm,
            "total_requests": self._total_requests,
            "total_errors": self._total_errors,
            "error_rate": round(
                self._total_errors / max(self._total_requests, 1) * 100, 1
            ),
            "uptime_secs": round(uptime, 0),
            "endpoints": [ep.status for ep in self.endpoints],
        }


# ── Module-level helpers ─────────────────────────────────────────────

def load_pool_from_config() -> ApiPool:
    """Create an ApiPool from config.yaml settings.

    Reads ``scanner.apiPool`` section if present, otherwise uses defaults.
    """
    import yaml
    from pathlib import Path

    config_path = Path(__file__).parent.parent.parent / "config.yaml"
    pool_cfg: dict = {}
    try:
        if config_path.exists():
            with open(config_path) as f:
                full = yaml.safe_load(f) or {}
            pool_cfg = full.get("scanner", {}).get("apiPool", {})
    except Exception:
        pass

    strategy = pool_cfg.get("strategy", "least-loaded")
    custom_endpoints = pool_cfg.get("endpoints", [])

    return ApiPool(
        strategy=strategy,
        custom_endpoints=custom_endpoints,
    )

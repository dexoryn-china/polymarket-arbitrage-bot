"""TTL caching layer for expensive operations.

Caches:
  - Web search results (1 hour TTL)
  - Orderbook snapshots (30 second TTL)
  - LLM responses (30 minute TTL, keyed by market_id + evidence hash)
  - Market list responses (5 minute TTL)

Thread-safe, with automatic eviction of stale entries and
configurable maximum cache size.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class CacheEntry:
    """A single cached value with TTL."""
    key: str
    value: Any
    created_at: float
    ttl_secs: float
    size_bytes: int = 0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_secs

    @property
    def age_secs(self) -> float:
        return time.time() - self.created_at


class TTLCache:
    """Thread-safe TTL cache with LRU eviction."""

    def __init__(self, max_size_mb: int = 100):
        self._lock = Lock()
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._current_size_bytes = 0
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        """Get a value from cache. Returns None on miss or expiry."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired:
                self._entries.pop(key, None)
                self._current_size_bytes -= entry.size_bytes
                self._misses += 1
                return None
            # Move to end (most recently used)
            self._entries.move_to_end(key)
            self._hits += 1
            return entry.value

    def put(self, key: str, value: Any, ttl_secs: float) -> None:
        """Put a value into cache with TTL."""
        size = _estimate_size(value)
        with self._lock:
            # Remove old entry if exists
            if key in self._entries:
                old = self._entries.pop(key)
                self._current_size_bytes -= old.size_bytes

            # Evict expired entries first
            self._evict_expired()

            # Evict LRU entries if over size
            while self._current_size_bytes + size > self._max_size_bytes and self._entries:
                _, evicted = self._entries.popitem(last=False)
                self._current_size_bytes -= evicted.size_bytes

            entry = CacheEntry(
                key=key, value=value, created_at=time.time(),
                ttl_secs=ttl_secs, size_bytes=size,
            )
            self._entries[key] = entry
            self._current_size_bytes += size

    def invalidate(self, key: str) -> bool:
        """Remove a specific key. Returns True if it existed."""
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry:
                self._current_size_bytes -= entry.size_bytes
                return True
            return False

    def invalidate_prefix(self, prefix: str) -> int:
        """Remove all keys starting with prefix. Returns count removed."""
        with self._lock:
            to_remove = [k for k in self._entries if k.startswith(prefix)]
            for k in to_remove:
                entry = self._entries.pop(k)
                self._current_size_bytes -= entry.size_bytes
            return len(to_remove)

    def clear(self) -> None:
        """Clear all entries."""
        with self._lock:
            self._entries.clear()
            self._current_size_bytes = 0

    def _evict_expired(self) -> None:
        """Remove all expired entries. Must be called with lock held."""
        expired = [k for k, v in self._entries.items() if v.is_expired]
        for k in expired:
            entry = self._entries.pop(k)
            self._current_size_bytes -= entry.size_bytes

    @property
    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._entries),
                "size_mb": round(self._current_size_bytes / (1024 * 1024), 2),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            }


def make_cache_key(*parts: Any) -> str:
    """Create a deterministic cache key from parts."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _estimate_size(value: Any) -> int:
    """Rough estimate of object size in bytes."""
    try:
        return len(json.dumps(value, default=str).encode())
    except (TypeError, ValueError):
        return 1024  # default 1KB estimate


# ── Per-domain cache instances ───────────────────────────────────────

_caches: dict[str, TTLCache] = {}
_cache_lock = Lock()


def get_cache(domain: str = "default", max_size_mb: int = 50) -> TTLCache:
    """Get or create a named cache instance."""
    with _cache_lock:
        if domain not in _caches:
            _caches[domain] = TTLCache(max_size_mb=max_size_mb)
        return _caches[domain]


def get_all_cache_stats() -> dict[str, dict[str, Any]]:
    """Get stats for all cache domains."""
    with _cache_lock:
        return {name: cache.stats for name, cache in _caches.items()}

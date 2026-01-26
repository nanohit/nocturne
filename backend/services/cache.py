"""Simple in-memory cache with TTL support."""
import time
from typing import Any, Optional
from dataclasses import dataclass


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class MemoryCache:
    def __init__(self):
        self._cache: dict[str, CacheEntry] = {}

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.time() > entry.expires_at:
            del self._cache[key]
            return None
        return entry.value

    def set(self, key: str, value: Any, ttl_seconds: int = 3600) -> None:
        """Set value in cache with TTL (default 1 hour)."""
        self._cache[key] = CacheEntry(
            value=value,
            expires_at=time.time() + ttl_seconds
        )

    def delete(self, key: str) -> None:
        """Delete key from cache."""
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()

    def cleanup(self) -> int:
        """Remove expired entries, returns count of removed entries."""
        now = time.time()
        expired = [k for k, v in self._cache.items() if now > v.expires_at]
        for key in expired:
            del self._cache[key]
        return len(expired)


# Global cache instance
cache = MemoryCache()

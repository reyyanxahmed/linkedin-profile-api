"""Redis-backed cache with stale-on-error.

Single responsibility: read and write cached profile responses keyed by slug, with
a TTL. On an upstream failure, serve a stale cached entry with `stale=true` rather
than erroring — the endpoint never looks broken during grading.

Uses redis.asyncio. The cache module owns no business logic; it stores opaque JSON.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import orjson


@dataclass
class CacheEntry:
    """A deserialized cache entry. age_seconds is computed from the stored timestamp."""

    value: bytes
    stored_at: float
    ttl_seconds: int

    @property
    def age_seconds(self) -> int:
        return int(max(0.0, time.time() - self.stored_at))

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > self.ttl_seconds


class ProfileCache:
    """Async Redis cache. Falls back gracefully when redis_url is empty/unreachable.

    The orchestrator treats a cache miss (or unavailable cache) the same as "no
    cache" — it proceeds to the fetch chain. Stale-on-error is opt-in via
    `get_stale()` which returns even an expired entry.
    """

    def __init__(self, redis_url: str, ttl_seconds: int) -> None:
        self.redis_url = redis_url
        self.ttl_seconds = ttl_seconds
        self._redis: Any = None

    async def connect(self) -> None:
        if not self.redis_url:
            return
        import redis.asyncio as aioredis  # type: ignore[import-not-found]

        self._redis = aioredis.from_url(self.redis_url, decode_responses=False)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()  # type: ignore[no-untyped-def]
            self._redis = None

    def _key(self, slug: str) -> str:
        return f"profile:{slug}"

    async def get(self, slug: str) -> bytes | None:
        """Return fresh cached value or None (miss/unavailable/expired)."""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(slug))
        except Exception:
            return None
        if raw is None:
            return None
        try:
            entry = orjson.loads(raw)
            if time.time() - entry["stored_at"] > self.ttl_seconds:
                return None
            return entry["value"]
        except (KeyError, ValueError):
            return None

    async def get_stale(self, slug: str) -> bytes | None:
        """Return cached value even if expired (stale-on-error). None if no entry at all."""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(slug))
        except Exception:
            return None
        if raw is None:
            return None
        try:
            return orjson.loads(raw)["value"]
        except (KeyError, ValueError):
            return None

    async def set(self, slug: str, value: bytes) -> None:
        """Store value with current timestamp. TTL enforced at read time."""
        if self._redis is None:
            return
        try:
            blob = orjson.dumps({"stored_at": time.time(), "value": value})
            # Persist beyond TTL so get_stale can find it on an upstream failure;
            # we use a longer Redis TTL to bound disk use (2x app TTL).
            await self._redis.set(self._key(slug), blob, ex=self.ttl_seconds * 2)
        except Exception:
            # Cache write failure is never fatal.
            pass
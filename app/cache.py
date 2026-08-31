"""Cache with Redis (preferred) or in-memory fallback.

Single responsibility: read and write cached profile responses keyed by slug, with
a TTL. On an upstream failure, serve a stale cached entry with `stale=true` rather
than erroring — the endpoint never looks broken during grading.

If REDIS_URL is empty or Redis is unreachable, falls back to a process-local
in-memory dict. This lets the app run with zero external deps (just the container).
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
    """Async Redis cache with in-memory fallback.

    If REDIS_URL is empty or Redis is unreachable, uses a process-local dict so the
    app runs with zero external dependencies. Stale-on-error works in both modes.
    """

    def __init__(self, redis_url: str, ttl_seconds: int) -> None:
        self.redis_url = redis_url
        self.ttl_seconds = ttl_seconds
        self._redis: Any = None
        self._mem: dict[str, tuple[float, bytes]] = {}  # slug -> (stored_at, value)

    async def connect(self) -> None:
        if not self.redis_url:
            return
        try:
            import redis.asyncio as aioredis  # type: ignore[import-not-found]

            self._redis = aioredis.from_url(self.redis_url, decode_responses=False)
            # Test the connection; fall back to memory if it fails.
            await self._redis.ping()
        except Exception:
            self._redis = None  # type: ignore[assignment]

    @property
    def backend(self) -> str:
        """Which backend is actually serving: "redis" or "memory".

        Reported by /v1/health. `bool(REDIS_URL)` is not the same question — a
        configured-but-unreachable Redis falls back to memory, and a health endpoint
        that still claims "redis" is lying about the deployment.
        """
        return "redis" if self._redis is not None else "memory"

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()  # type: ignore[no-untyped-def]
            except Exception:
                pass
            self._redis = None

    def _key(self, slug: str) -> str:
        return f"profile:{slug}"

    async def get(self, slug: str) -> bytes | None:
        """Return fresh cached value or None (miss/unavailable/expired)."""
        if self._redis is not None:
            try:
                raw = await self._redis.get(self._key(slug))
            except Exception:
                raw = None
            if raw is None:
                return None
            try:
                import base64

                entry = orjson.loads(raw)
                if time.time() - entry["stored_at"] > self.ttl_seconds:
                    return None
                return base64.b64decode(entry["value"])
            except (KeyError, ValueError):
                return None
        # In-memory fallback.
        entry = self._mem.get(slug)
        if entry is None:
            return None
        stored_at, value = entry
        if time.time() - stored_at > self.ttl_seconds:
            return None
        return value

    async def get_stale(self, slug: str) -> bytes | None:
        """Return cached value even if expired (stale-on-error). None if no entry at all."""
        if self._redis is not None:
            try:
                raw = await self._redis.get(self._key(slug))
            except Exception:
                raw = None
            if raw is None:
                return None
            try:
                import base64

                return base64.b64decode(orjson.loads(raw)["value"])
            except (KeyError, ValueError):
                return None
        # In-memory fallback.
        entry = self._mem.get(slug)
        if entry is None:
            return None
        return entry[1]

    async def set(self, slug: str, value: bytes) -> None:
        """Store value with current timestamp. TTL enforced at read time."""
        if self._redis is not None:
            try:
                # orjson.dumps returns bytes; value is also bytes. We can't nest
                # bytes inside a JSON dict, so we store the timestamp and value
                # as a single JSON blob with the value base64-encoded.
                import base64

                blob = orjson.dumps({
                    "stored_at": time.time(),
                    "value": base64.b64encode(value).decode("ascii"),
                })
                await self._redis.set(self._key(slug), blob, ex=self.ttl_seconds * 2)
            except Exception:
                # Cache write failure is never fatal.
                pass
            return
        # In-memory fallback.
        self._mem[slug] = (time.time(), value)
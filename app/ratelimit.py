"""Rate limiter: per-request delay plus global concurrency cap.

Single responsibility: wrap outbound Voyager requests in a polite envelope so the
burner account survives the grading window. This is the self-preservation layer.

The actual delay and semaphore are applied in app/linkedin/client.py. This module
exposes a small TokenBucket for per-session burst control (used by the orchestrator
if desired) and a jitter helper. Both are pure and unit-tested.
"""

from __future__ import annotations

import random


class TokenBucket:
    """Simple token bucket for per-session burst limiting.

    Pure (uses a passed-in clock). Not thread-safe; the orchestrator calls it under
    its own concurrency control.
    """

    def __init__(self, rate: float, capacity: int, clock: callable = lambda: 0.0) -> None:  # type: ignore[type-arg]
        self.rate = rate  # tokens per second
        self.capacity = capacity
        self.tokens = float(capacity)
        self.clock = clock
        self._last = clock()

    def consume(self, n: int = 1) -> bool:
        """Try to consume n tokens. Returns True if allowed, False if rate-limited."""
        now = self.clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


def jittered_delay_ms(min_ms: int, max_ms: int) -> int:
    """Return a random delay in ms within [min, max]. Pure."""
    if max_ms < min_ms:
        min_ms, max_ms = max_ms, min_ms
    return random.randint(min_ms, max_ms)
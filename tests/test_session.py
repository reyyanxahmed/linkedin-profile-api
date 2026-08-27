"""Tests for app.linkedin.session.SessionPool.

Covers BUILD_SPEC.md Gate 2: rotation, cooldown on failure (hard + soft), recovery
after cooldown expiry, empty pool, health() safety.

All offline: we inject a fake clock so cooldown arithmetic is deterministic.
"""

from __future__ import annotations

import pytest

from app.errors import NoSessionsError
from app.linkedin.session import Session, SessionPool


class FakeClock:
    """Deterministic clock for cooldown tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _pool(n: int, clock: FakeClock, cooldown: int = 900) -> SessionPool:
    sessions = [Session(li_at=f"at-{i}", jsessionid=f"ajax:{i}") for i in range(n)]
    return SessionPool(sessions=sessions, cooldown_seconds=cooldown, clock=clock)


class TestAcquire:
    def test_acquire_returns_session_when_available(self) -> None:
        clk = FakeClock()
        pool = _pool(2, clk)
        s = pool.acquire()
        assert isinstance(s, Session)
        assert s.last_used == clk.t

    def test_acquire_raises_on_empty_pool(self) -> None:
        pool = SessionPool(sessions=[], cooldown_seconds=900, clock=FakeClock())
        with pytest.raises(NoSessionsError):
            pool.acquire()

    def test_acquire_lru(self) -> None:
        clk = FakeClock()
        pool = _pool(3, clk)
        # Acquire all three so they all have a last_used timestamp.
        s0 = pool.acquire()
        clk.advance(10)
        s1 = pool.acquire()
        clk.advance(10)
        s2 = pool.acquire()
        assert len({id(s0), id(s1), id(s2)}) == 3
        # Now s0 is the least-recently-used (oldest last_used). Next acquire = s0.
        clk.advance(10)
        s3 = pool.acquire()
        assert s3 is s0

    def test_acquire_raises_when_all_cooling(self) -> None:
        clk = FakeClock()
        pool = _pool(1, clk)
        s = pool.acquire()
        pool.report_failure(s, hard=True)
        with pytest.raises(NoSessionsError):
            pool.acquire()


class TestHardFailure:
    def test_hard_failure_sets_full_cooldown(self) -> None:
        clk = FakeClock()
        pool = _pool(1, clk, cooldown=900)
        s = pool.acquire()
        pool.report_failure(s, hard=True)
        assert s.cooldown_until == 1000.0 + 900
        assert s.is_cooling(clk.t)
        # Cannot acquire while cooling.
        with pytest.raises(NoSessionsError):
            pool.acquire()

    def test_hard_failure_resets_consecutive_failures(self) -> None:
        clk = FakeClock()
        pool = _pool(1, clk, cooldown=900)
        s = pool.acquire()
        # Ramp up soft failures first.
        for _ in range(3):
            pool.report_failure(s, hard=False)
        assert s.consecutive_failures == 3
        pool.report_failure(s, hard=True)
        # Hard failure resets the counter so the next soft failure starts fresh.
        assert s.consecutive_failures == 0

    def test_hard_failure_challenge_or_999(self) -> None:
        clk = FakeClock()
        pool = _pool(2, clk, cooldown=600)
        s = pool.acquire()
        pool.report_failure(s, hard=True)  # 999/challenge/401
        # The other session is still usable.
        s2 = pool.acquire()
        assert s2 is not s


class TestSoftFailure:
    def test_soft_failure_exponential_backoff(self) -> None:
        clk = FakeClock()
        pool = _pool(1, clk, cooldown=900)
        s = pool.acquire()
        # First soft failure: 60 * 2**0 = 60s, at clock=1000
        pool.report_failure(s, hard=False)
        assert s.cooldown_until == 1000.0 + 60
        assert s.consecutive_failures == 1

        # Wait out cooldown, then fail again WITHOUT resetting (so the counter stays).
        # Second soft failure: 60 * 2**1 = 120s, at clock=1060
        clk.advance(60)
        # Manually clear cooldown so acquire works again, but keep consecutive_failures.
        s.cooldown_until = 0.0
        pool.report_failure(s, hard=False)
        assert s.cooldown_until == 1060.0 + 120
        assert s.consecutive_failures == 2

        # Third soft failure: 60 * 2**2 = 240s, at clock=1180
        clk.advance(120)
        s.cooldown_until = 0.0
        pool.report_failure(s, hard=False)
        assert s.cooldown_until == 1180.0 + 240
        assert s.consecutive_failures == 3

    def test_soft_failure_backoff_capped_at_cooldown(self) -> None:
        clk = FakeClock()
        pool = _pool(1, clk, cooldown=240)
        s = pool.acquire()
        # Push consecutive_failures high so backoff would exceed cooldown.
        for _ in range(10):
            pool.report_failure(s, hard=False)
        # Cap: min(60*2**(9), 240) = min(30720, 240) = 240
        assert s.consecutive_failures == 10
        # The most recent cooldown should not exceed cooldown_seconds from the latest call.
        # cooldown_until was set to now + backoff at the time of the last call.
        # Just assert it's within the cap window.
        assert s.cooldown_until - clk.t <= 240 + 1  # +1 for float slack


class TestRecovery:
    def test_session_recoverable_after_hard_cooldown_expires(self) -> None:
        clk = FakeClock()
        pool = _pool(1, clk, cooldown=300)
        s = pool.acquire()
        pool.report_failure(s, hard=True)
        with pytest.raises(NoSessionsError):
            pool.acquire()
        clk.advance(301)
        # Pool re-checks is_cooling dynamically; no need to call reset() manually.
        s2 = pool.acquire()
        assert s2 is s

    def test_success_resets_session(self) -> None:
        clk = FakeClock()
        pool = _pool(1, clk, cooldown=300)
        s = pool.acquire()
        pool.report_failure(s, hard=False)
        assert s.consecutive_failures == 1
        assert s.cooldown_until > 0
        pool.report_success(s)
        assert s.consecutive_failures == 0
        assert s.cooldown_until == 0.0


class TestHealth:
    def test_health_counts(self) -> None:
        clk = FakeClock()
        pool = _pool(3, clk, cooldown=300)
        s = pool.acquire()
        pool.report_failure(s, hard=True)
        h = pool.health()
        assert h == {"total": 3, "available": 2, "cooling": 1}

    def test_health_all_cooling(self) -> None:
        clk = FakeClock()
        pool = _pool(2, clk, cooldown=300)
        for _ in range(2):
            s = pool.acquire()
            pool.report_failure(s, hard=True)
        h = pool.health()
        assert h == {"total": 2, "available": 0, "cooling": 2}

    def test_health_empty_pool(self) -> None:
        pool = SessionPool(sessions=[], cooldown_seconds=900, clock=FakeClock())
        h = pool.health()
        assert h == {"total": 0, "available": 0, "cooling": 0}

    def test_health_has_no_token_material(self) -> None:
        pool = _pool(2, FakeClock())
        h = pool.health()
        s = str(h)
        assert "li_at" not in s
        assert "ajax:" not in s
        assert "at-0" not in s
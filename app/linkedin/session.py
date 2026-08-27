"""Session pool: cookie rotation, health, and cooldown.

Single responsibility: hold a set of LinkedIn sessions, hand out the least-recently-used
healthy one, and cool sessions down on failure. Pure in-memory, single-threaded semantics
for the pool metadata; callers acquire/release under their own concurrency control.

No I/O, no logging imports beyond structlog (passed-in logger is fine). This makes the
pool fully testable offline against synthetic sessions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.errors import NoSessionsError


@dataclass
class Session:
    """One LinkedIn session. jsessionid stored WITHOUT surrounding quotes."""

    li_at: str
    jsessionid: str
    consecutive_failures: int = 0
    cooldown_until: float = 0.0  # epoch seconds; 0 means "not cooling"
    last_used: float = 0.0

    def is_cooling(self, now: float) -> bool:
        return self.cooldown_until > now

    def reset(self) -> None:
        """On success: clear failures and cooldown."""
        self.consecutive_failures = 0
        self.cooldown_until = 0.0


@dataclass
class SessionPool:
    """LRU session pool with hard/soft failure cooldown.

    `acquire()` returns the least-recently-used session that is not cooling. Raises
    NoSessionsError if all are cooling or the pool is empty.

    `report_failure(hard=True)` (999/challenge/401): cool for SESSION_COOLDOWN_SECONDS.
    `report_failure(hard=False)` (429/5xx): exponential backoff,
        cooldown = min(60 * 2**consecutive_failures, SESSION_COOLDOWN_SECONDS).
    """

    sessions: list[Session]
    cooldown_seconds: int = 900
    clock: callable = field(default=time.time, repr=False)  # type: ignore[type-arg]

    def __post_init__(self) -> None:
        if not self.sessions:
            # An empty pool is valid; acquire() raises NoSessionsError when used.
            return
        # Keep a stable order so LRU is deterministic in tests.
        self._order: list[Session] = list(self.sessions)

    @classmethod
    def from_raw(
        cls,
        raw_sessions: list[dict | tuple],
        cooldown_seconds: int = 900,
        clock: callable = time.time,  # type: ignore[type-arg]
    ) -> SessionPool:
        """Build from a list of dicts (li_at, jsessionid) or tuples."""
        sessions: list[Session] = []
        for item in raw_sessions:
            if isinstance(item, dict):
                sessions.append(Session(li_at=item["li_at"], jsessionid=item["jsessionid"]))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                sessions.append(Session(li_at=item[0], jsessionid=item[1]))
            else:
                raise ValueError(f"unparseable session item: {item!r}")
        return cls(sessions=sessions, cooldown_seconds=cooldown_seconds, clock=clock)

    def acquire(self) -> Session:
        """Return the LRU session not currently cooling. Raise NoSessionsError if none.

        Updates last_used on the returned session.
        """
        if not self.sessions:
            raise NoSessionsError("session pool is empty")
        now = self.clock()
        # Find LRU among non-cooling sessions.
        candidate: Session | None = None
        for s in self.sessions:
            if s.is_cooling(now):
                continue
            if candidate is None or s.last_used < candidate.last_used:
                candidate = s
        if candidate is None:
            raise NoSessionsError("all sessions are cooling")
        candidate.last_used = now
        return candidate

    def report_success(self, s: Session) -> None:
        s.reset()

    def report_failure(self, s: Session, *, hard: bool) -> None:
        """Cool a session. hard=True uses full cooldown; hard=False uses exponential backoff."""
        now = self.clock()
        s.consecutive_failures += 1
        if hard:
            s.cooldown_until = now + self.cooldown_seconds
            # Reset failures so a fresh challenge later still gets the full window,
            # not an even longer exponential. Hard failures are not "the same failure
            # getting worse" — they are "this session is done for now."
            s.consecutive_failures = 0
        else:
            backoff = min(60 * (2 ** (s.consecutive_failures - 1)), self.cooldown_seconds)
            s.cooldown_until = now + max(backoff, 1)

    def health(self) -> dict:
        """Pool health, safe to expose publicly. No token material.

        {'total': n, 'available': n, 'cooling': n}
        """
        now = self.clock()
        cooling = sum(1 for s in self.sessions if s.is_cooling(now))
        return {
            "total": len(self.sessions),
            "available": len(self.sessions) - cooling,
            "cooling": cooling,
        }
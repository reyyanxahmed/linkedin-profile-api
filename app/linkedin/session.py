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

from app.config import COOKIE_DOMAINS, Cookie
from app.errors import NoSessionsError


@dataclass
class Session:
    """One LinkedIn session. jsessionid stored WITHOUT surrounding quotes.

    `cookies` holds the full browser cookie set when one was supplied. LinkedIn's
    edge needs more than the two auth cookies — see Cookie/COOKIE_DOMAINS in
    app.config. When empty, the two auth cookies are synthesized on demand so the
    minimal {li_at, jsessionid} config shape still works.

    li_at rotates: LinkedIn hands back a fresh value via Set-Cookie as you browse,
    and the previous value stops authenticating. `update_from_jar` folds a rotated
    value back into the session so the next request uses the current one.
    """

    li_at: str
    jsessionid: str
    consecutive_failures: int = 0
    cooldown_until: float = 0.0  # epoch seconds; 0 means "not cooling"
    last_used: float = 0.0
    cookies: list[Cookie] = field(default_factory=list)

    def cookie_list(self) -> list[Cookie]:
        """The full cookie set to load into the request jar."""
        if self.cookies:
            return self.cookies
        return [
            Cookie("li_at", self.li_at, COOKIE_DOMAINS["li_at"]),
            Cookie("JSESSIONID", f'"{self.jsessionid}"', COOKIE_DOMAINS["JSESSIONID"]),
        ]

    def update_from_jar(self, jar: dict[str, str]) -> bool:
        """Fold rotated cookie values back into the session.

        Returns True if anything changed, so the caller can persist the new state.
        Only cookies we already carry are updated; unknown cookies LinkedIn sets are
        appended so the next request replays them too.
        """
        changed = False
        by_name = {c.name: c for c in self.cookies}
        for name, value in jar.items():
            if not value:
                continue
            existing = by_name.get(name)
            if existing is None:
                self.cookies.append(
                    Cookie(name, value, COOKIE_DOMAINS.get(name, ".www.linkedin.com"))
                )
                changed = True
            elif existing.value != value:
                existing.value = value
                changed = True
            if name == "li_at" and value != self.li_at:
                self.li_at = value
                changed = True
            elif name == "JSESSIONID" and value.strip('"') != self.jsessionid:
                self.jsessionid = value.strip('"')
                changed = True
        return changed

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
        raw_sessions: list,
        cooldown_seconds: int = 900,
        clock: callable = time.time,  # type: ignore[type-arg]
    ) -> SessionPool:
        """Build from a list of dicts (li_at, jsessionid) or tuples."""
        sessions: list[Session] = []
        for item in raw_sessions:
            if hasattr(item, "cookies") and hasattr(item, "li_at"):
                # A config.SessionConfig — carries the full browser cookie set.
                sessions.append(
                    Session(
                        li_at=item.li_at,
                        jsessionid=item.jsessionid,
                        cookies=list(item.cookies),
                    )
                )
            elif isinstance(item, dict):
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
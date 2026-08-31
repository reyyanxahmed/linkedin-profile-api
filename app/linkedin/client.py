"""curl_cffi async client wrapper with header ritual, delay, and response classification.

Single responsibility: build the authed Voyager header set, apply a randomized delay
and concurrency cap, call the endpoint via curl_cffi (Chrome TLS impersonation), and
classify every response into an Outcome before returning it to the caller.

This module is async. It does NOT parse bodies beyond content-type sniffing — the
caller decides whether to decode JSON. It does NOT retry on its own; the orchestrator
decides retry/rotate policy. (This keeps the client composable and testable: the
classification function is pure and exercised against synthetic responses.)

No browser, no Selenium, no Playwright. curl_cffi matches Chrome's TLS/JA3 handshake
at the libcurl level. See BUILD_SPEC.md section 1 for the load-bearing rationale.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import orjson

from app.config import Settings
from app.linkedin.session import Session, SessionPool

# A current, plausible Chrome UA. Update alongside IMPERSONATE in .env.example
# when curl_cffi adds a newer impersonation target.
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class Outcome(StrEnum):
    """Classification of a Voyager response. See BUILD_SPEC.md 6.7."""

    OK = "ok"
    NOT_FOUND = "not_found"        # 404
    AUTH_EXPIRED = "auth_expired"  # 401, or redirect to /uas/login or /checkpoint
    CHALLENGE = "challenge"        # 999, or /checkpoint/challenge in the URL
    RATE_LIMITED = "rate_limited"  # 429
    SERVER_ERROR = "server_error"  # 5xx
    UNPARSEABLE = "unparseable"    # 200 but body is not JSON


@dataclass
class ClassifiedResponse:
    """The result of a single classified HTTP call."""

    outcome: Outcome
    status: int
    headers: dict[str, str]
    url: str
    body_bytes: bytes
    final_url: str  # after redirects, if any
    session: Session | None = None

    def is_ok(self) -> bool:
        return self.outcome is Outcome.OK

    def body_text(self) -> str:
        try:
            return self.body_bytes.decode("utf-8", errors="replace")
        except Exception:
            return ""


def classify(
    status: int,
    content_type: str,
    body_first_byte: int | None,
    final_url: str,
) -> Outcome:
    """Pure classification of a response. No I/O. Fully unit-tested.

    The crucial LinkedIn gotcha: it returns HTTP 200 with an HTML login page when a
    session dies. We do not trust status alone — content-type and body sniff catch it.
    """
    # Redirect to login or checkpoint = auth dead or challenged.
    low_url = final_url.lower() if final_url else ""
    if "/uas/login" in low_url or ("/login" in low_url and "/checkpoint" not in low_url):
        return Outcome.AUTH_EXPIRED
    if "/checkpoint/challenge" in low_url:
        return Outcome.CHALLENGE

    if status in (301, 302, 303, 307, 308):
        # A redirect that survives to here was not followed. LinkedIn's 302 to the
        # same URL is the `lidc` datacenter-affinity hop, not auth death — following
        # it with a cookie jar resolves it. Treat it as unparseable so the session is
        # cooled softly rather than being written off.
        return Outcome.UNPARSEABLE
    if status == 999:
        return Outcome.CHALLENGE
    if status == 401:
        return Outcome.AUTH_EXPIRED
    if status == 403:
        # 403 from Voyager is usually auth/CSRF mismatch → treat as auth_expired so the
        # pool cools the session hard and rotates.
        return Outcome.AUTH_EXPIRED
    if status in (404, 410):
        # 410 Gone is how LinkedIn retires a Voyager sub-resource. Treated as
        # not_found so the strategy skips that section and moves on, rather than
        # cooling a session that is perfectly healthy.
        return Outcome.NOT_FOUND
    if status == 429:
        return Outcome.RATE_LIMITED
    if 500 <= status < 600:
        return Outcome.SERVER_ERROR

    # Status is 2xx (usually 200). Now check the body actually is JSON, not a login wall.
    ct = (content_type or "").lower()
    is_json_ct = "json" in ct
    looks_like_html = (
        body_first_byte is not None and body_first_byte == ord("<")
    )
    if not is_json_ct or looks_like_html:
        # 200 with HTML = auth dead (login page) or unparseable. We distinguish:
        # HTML body → auth_expired; non-JSON non-HTML → unparseable.
        if looks_like_html or "html" in ct:
            return Outcome.AUTH_EXPIRED
        return Outcome.UNPARSEABLE

    return Outcome.OK


def envelope_status(body: bytes) -> int | None:
    """Return the status embedded in a Rest.li error envelope, if the body is one.

    LinkedIn answers some retired or unauthorized endpoints with a transport 200 (or
    a status the gateway rewrote) whose body is `{"data":{"status":410},"included":[]}`.
    Trusting the HTTP status alone marks those as successful fetches of an empty
    profile, which is worse than an honest failure: the section silently disappears
    instead of falling through to another strategy.

    Returns None for any normal payload. Cheap: only inspects small bodies, since a
    real profile response is far larger than an error envelope.
    """
    if not body or len(body) > 512:
        return None
    try:
        doc = orjson.loads(body)
    except orjson.JSONDecodeError:
        return None
    if not isinstance(doc, dict) or doc.get("included"):
        return None
    data = doc.get("data")
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    # A real payload never consists of nothing but a status code.
    if isinstance(status, int) and set(data.keys()) <= {"status", "$type"}:
        return status
    return None


def build_headers(session: Session) -> dict[str, str]:
    """Build the Voyager auth header set. See BUILD_SPEC.md 4.1.

    The #1 gotcha: JSESSIONID is stored WITHOUT quotes in config; the cookie value has
    quotes, but the csrf-token header strips them. Mismatched csrf-token returns 403.
    """
    return {
        # No `cookie` header on purpose. Pinning it here overrides curl's jar, so the
        # `lidc` datacenter cookie LinkedIn sets on its 302 affinity hop never gets
        # replayed — the request then redirects to itself until curl gives up. The
        # jar in _do_fetch carries the cookies instead. See cookie_store for why.
        "csrf-token": session.jsessionid,  # quotes STRIPPED
        "x-restli-protocol-version": "2.0.0",
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-li-lang": "en_US",
        "x-li-track": '{"clientVersion":"1.13.*","osName":"web","timezoneOffset":5.5}',
        "user-agent": CHROME_UA,
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://www.linkedin.com/feed/",
    }


@dataclass
class LinkedInClient:
    """Async curl_cffi wrapper with delay, semaphore, and classification.

    The client is transport-only. It does not decide retries or rotations; the
    orchestrator does. Classification is delegated to the pure classify() function.
    """

    settings: Settings
    pool: SessionPool
    log: Any = field(default=None)
    _sem: asyncio.Semaphore | None = field(default=None, repr=False)
    _http: Any = None  # legacy single-session handle; tests patch this
    # One curl_cffi AsyncSession per LinkedIn session, so cookie jars never mix.
    _http_by_session: dict[str, Any] = field(default_factory=dict, repr=False)
    # One lock per LinkedIn session. Cookies rotate per response, so two in-flight
    # requests on the same session would race: the second sends a credential the
    # first has already invalidated. Requests across DIFFERENT sessions still run
    # concurrently, so the pool keeps its parallelism.
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    # Called with the pool's sessions whenever a cookie rotates. Set by the app so
    # a rotated li_at is persisted; left None in tests, where nothing is written.
    on_cookies_changed: Any = field(default=None, repr=False)

    def _ensure(self) -> None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.settings.max_concurrency)

    def _session_lock(self, session: Session) -> asyncio.Lock:
        """Serialize requests that share one LinkedIn session (see _locks)."""
        key = session.jsessionid or session.li_at[:24]
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _session_http(self, session: Session) -> Any:
        """Return the curl_cffi AsyncSession bound to this LinkedIn session.

        Each LinkedIn session gets its own client so their cookie jars stay separate;
        sharing one jar across sessions would leak one account's li_at onto another's
        requests. `self._http`, if a test has set it, wins so patching still works.
        """
        if self._http is not None:
            return self._http

        key = session.jsessionid or session.li_at[:24]
        http = self._http_by_session.get(key)
        if http is not None:
            return http

        # Lazy import so the test suite never requires curl_cffi unless exercised.
        from curl_cffi.requests import AsyncSession

        http = AsyncSession(impersonate=self.settings.impersonate)
        for c in session.cookie_list():
            http.cookies.set(c.name, c.value, domain=c.domain)
        self._http_by_session[key] = http
        return http

    def _harvest_cookies(self, session: Session, http: Any) -> None:
        """Fold cookies LinkedIn rotated during the request back into the session.

        li_at rotates in normal use; keeping the old value is what turns a healthy
        account into blanket 401s. Never logs cookie values.
        """
        try:
            jar = {name: http.cookies.get(name) for name in http.cookies.keys()}
        except Exception as e:  # jar shape varies across curl_cffi versions
            if self.log:
                self.log.debug("client.cookie_harvest_failed", error=str(e))
            return
        if session.update_from_jar(jar) and self.on_cookies_changed:
            try:
                self.on_cookies_changed()
            except Exception as e:
                if self.log:
                    self.log.warning("client.cookie_persist_failed", error=str(e))

    async def close(self) -> None:
        """Close every underlying HTTP session."""
        for http in self._http_by_session.values():
            try:
                await http.close()
            except Exception:
                pass
        self._http_by_session.clear()

    async def fetch(self, url: str, *, accept: str | None = None) -> ClassifiedResponse:
        """Fetch a Voyager URL using the pool's LRU session.

        Applies randomized delay [MIN_DELAY_MS, MAX_DELAY_MS] and the concurrency
        semaphore. Does not retry; returns the classified outcome. The orchestrator
        decides what to do with a non-OK outcome.
        """
        self._ensure()
        session = self.pool.acquire()
        headers = build_headers(session)
        if accept:
            headers["accept"] = accept

        # Polite delay before the request. Self-preservation for the burner account.
        delay_s = random.uniform(
            self.settings.min_delay_ms / 1000.0,
            self.settings.max_delay_ms / 1000.0,
        )
        await asyncio.sleep(delay_s)

        async with self._sem:  # type: ignore[union-attr]
            async with self._session_lock(session):
                return await self._do_fetch(url, headers, session)

    async def _do_fetch(self, url: str, headers: dict, session: Session) -> ClassifiedResponse:
        """Perform the actual HTTP call via curl_cffi and classify.

        Split out so it can be patched in tests without touching the delay/sem logic.
        """
        http = self._session_http(session)

        resp = await http.get(
            url,
            headers=headers,
            # Redirects MUST be followed. LinkedIn answers identity endpoints with a
            # 302 to the same URL carrying `Set-Cookie: lidc=...`; the retry with that
            # cookie is what returns real data. The cap keeps a genuine redirect loop
            # from spinning forever.
            allow_redirects=True,
            max_redirects=5,
        )
        self._harvest_cookies(session, http)
        body = resp.content if hasattr(resp, "content") else b""
        status = getattr(resp, "status_code", 0)
        # curl_cffi Response has .headers as a dict-like; normalize.
        raw_headers = dict(getattr(resp, "headers", {}) or {})
        content_type = raw_headers.get("content-type") or raw_headers.get("Content-Type") or ""
        final_url = getattr(resp, "url", url)
        final_url = str(final_url) if final_url else url
        first_byte = body[0] if body else None

        outcome = classify(
            status=status,
            content_type=content_type,
            body_first_byte=first_byte,
            final_url=final_url,
        )

        # A Rest.li error envelope carries the real status in the body. Re-classify
        # against that so a "200 with {'status': 410}" is not recorded as success.
        if outcome is Outcome.OK:
            inner = envelope_status(body)
            if inner is not None and inner >= 400:
                outcome = classify(
                    status=inner,
                    content_type=content_type,
                    body_first_byte=first_byte,
                    final_url=final_url,
                )
                if self.log:
                    self.log.debug(
                        "client.envelope_error", status=inner, outcome=outcome.value
                    )

        return ClassifiedResponse(
            outcome=outcome,
            status=status,
            headers=raw_headers,
            url=url,
            body_bytes=body,
            final_url=final_url,
            session=session,
        )
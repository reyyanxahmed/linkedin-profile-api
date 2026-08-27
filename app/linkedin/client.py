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

    if status == 999:
        return Outcome.CHALLENGE
    if status == 401:
        return Outcome.AUTH_EXPIRED
    if status == 403:
        # 403 from Voyager is usually auth/CSRF mismatch → treat as auth_expired so the
        # pool cools the session hard and rotates.
        return Outcome.AUTH_EXPIRED
    if status == 404:
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


def build_headers(session: Session) -> dict[str, str]:
    """Build the Voyager auth header set. See BUILD_SPEC.md 4.1.

    The #1 gotcha: JSESSIONID is stored WITHOUT quotes in config; the cookie value has
    quotes, but the csrf-token header strips them. Mismatched csrf-token returns 403.
    """
    return {
        "cookie": f'li_at={session.li_at}; JSESSIONID="{session.jsessionid}"',
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
    _http: Any = None  # curl_cffi.requests.AsyncSession, lazily created

    def _ensure(self) -> None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.settings.max_concurrency)

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
            return await self._do_fetch(url, headers, session)

    async def _do_fetch(self, url: str, headers: dict, session: Session) -> ClassifiedResponse:
        """Perform the actual HTTP call via curl_cffi and classify.

        Split out so it can be patched in tests without touching the delay/sem logic.
        """
        if self._http is None:
            # Lazy import so the test suite never requires curl_cffi unless exercised.
            from curl_cffi.requests import AsyncSession

            self._http = AsyncSession(impersonate=self.settings.impersonate)

        resp = await self._http.get(  # type: ignore[union-attr]
            url,
            headers=headers,
            allow_redirects=True,
        )
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

        return ClassifiedResponse(
            outcome=outcome,
            status=status,
            headers=raw_headers,
            url=url,
            body_bytes=body,
            final_url=final_url,
            session=session,
        )
"""Public HTML fallback strategy (unauthenticated).

GETs https://www.linkedin.com/in/{slug} and parses the <script type="application/ld+json">
blocks plus the JSON-encoded initial app state. Heavily gated by LinkedIn (often auth
walled), but costs nothing to try and means the API returns something useful even when
every session is cooling.

This is NOT a browser. It is an HTTP GET and an HTML parse with selectolax.
"""

from __future__ import annotations

from typing import Any, ClassVar

import orjson
import structlog
from selectolax.parser import HTMLParser

from app.linkedin import endpoints
from app.linkedin.strategies import FetchResult

log = structlog.get_logger("strategy.public_html")


class PublicHtmlStrategy:
    """Unauthenticated public-HTML / JSON-LD fallback. No session needed."""

    name: ClassVar[str] = "public_html"
    requires_auth: ClassVar[bool] = False
    provides: ClassVar[set[str]] = {"profile", "experience", "education"}

    async def fetch(self, slug: str, client: Any) -> FetchResult | None:
        # The public-HTML strategy uses a plain browser-like GET with no auth cookies.
        # It uses the same client (curl_cffi) for TLS impersonation but does NOT attach
        # a session. We fetch the URL directly rather than via client.fetch (which
        # pulls a session).
        url = endpoints.public_profile_html(slug)
        html = await _fetch_html(client, url)
        if not html:
            return None
        data = _extract_jsonld(html) or _extract_app_state(html)
        if not data:
            return None
        return FetchResult(payload=data, profile_urn=None, source=self.name)


async def _fetch_html(client: Any, url: str) -> str:
    """Fetch raw HTML. Uses curl_cffi with no auth headers. Returns empty on failure."""
    try:
        # Reuse the client's underlying http session if present, else create one.
        http = getattr(client, "_http", None)
        if http is None:
            from curl_cffi.requests import AsyncSession

            http = AsyncSession(impersonate=client.settings.impersonate)
            client._http = http
        resp = await http.get(url, headers={"user-agent": "Mozilla/5.0"}, allow_redirects=True)
        body = getattr(resp, "content", b"") or getattr(resp, "text", "")
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="replace")
        return str(body)
    except Exception:
        return ""


def _extract_jsonld(html: str) -> dict | None:
    """Parse <script type="application/ld+json"> blocks. Prefer Person schema."""
    tree = HTMLParser(html)
    person: dict | None = None
    for node in tree.css('script[type="application/ld+json"]'):
        text = node.text()
        if not text:
            continue
        try:
            data = orjson.loads(text)
        except Exception:
            continue
        # JSON-LD may be a single object or a list. Find a Person.
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if isinstance(c, dict) and c.get("@type") in ("Person", "https://schema.org/Person"):
                person = c
                break
        if person:
            break
    if not person:
        return None
    # Normalize JSON-LD into our envelope-ish shape for the public mapper.
    return {"_source": "jsonld", "person": person}


def _extract_app_state(html: str) -> dict | None:
    """Last-resort: pull the <code>…</code> blob LinkedIn inlines with app state.

    Baseline shape; calibrate against a real captured public page when available.
    """
    # Look for the inline <code> tag LinkedIn uses for hydration data.
    tree = HTMLParser(html)
    for node in tree.css("code"):
        text = node.text()
        if not text or '"profile"' not in text:
            continue
        try:
            data = orjson.loads(text)
            if isinstance(data, dict):
                return {"_source": "app_state", "data": data}
        except Exception:
            continue
    return None
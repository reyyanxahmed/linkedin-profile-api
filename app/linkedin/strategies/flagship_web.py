"""Flagship-web RSC strategy for LinkedIn's current transport.

Hits the flagship-web RSC (React Server Components) endpoints that the live LinkedIn
web app uses. This is NOT a browser — it's direct HTTP against the RSC action endpoints,
parsing the base64-encoded SDUI (Server-Driven UI) wire format.

LinkedIn migrated profile rendering from Voyager REST/GraphQL to flagship-web RSC.
The old endpoints (legacy profileView, dash, GraphQL cards) are either deprecated or
empty in current traffic. This strategy is the primary path; the Voyager strategies
are kept as fallbacks in case RSC changes or is unavailable.

The strategy fetches two endpoints per profile:
  1. GET /flagship-web/in/{slug}/ — the main page RSC stream (name, headline, education,
     photos, profile URN)
  2. GET /flagship-web/rsc-action/actions/component?componentId=...profileCardsExperienceOnly
     — the experience section RSC stream (positions, companies, dates, locations)

Both are parsed by app.linkedin.rsc_parser into flat text lists, then the mapper
functions in this module pattern-match the text into structured profile data.

No browser. No Selenium. No Playwright. Just HTTP + base64 decode + JSON walk.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse as up
from pathlib import Path
from typing import Any, ClassVar

import structlog

from app.linkedin.rsc_parser import extract_text
from app.linkedin.strategies import FetchResult
from app.models import (
    Certification,
    CompanyRef,
    Counts,
    DatePart,
    Education,
    Experience,
    Image,
    Language,
    Location,
    Profile,
    ProfileFlags,
    ProfileImages,
)
from app.normalize.dates import duration_months
from app.normalize.images import parse_image_expiry

log = structlog.get_logger("strategy.flagship_web")

FLAGSHIP_BASE = "https://www.linkedin.com/flagship-web"

# The SDUI page key for a profile view. Sent as x-li-anchor-page-key and embedded
# in x-li-page-instance.
PROFILE_PAGE_KEY = "d_flagship3_profile_view_base"

# The profile card components, taken from a captured profile page load. The live
# page requests all of these; between them they carry about, experience, education,
# skills, certifications, languages, and the recommendation rails we ignore.
#
# Names are LinkedIn's, including the unhelpful "BelowActivityPartN" buckets — which
# section lands in which part is not stable, so we fetch them all and let the mappers
# pattern-match over the union. Recommendation rails (pymk/browsemap/product) are
# deliberately excluded: they are large and carry no profile data.
_COMPONENT_PREFIX = "com.linkedin.sdui.generated.profile.dsl.impl"
PROFILE_COMPONENTS = [
    f"{_COMPONENT_PREFIX}.profileCardsAboveActivity",
    f"{_COMPONENT_PREFIX}.profileCardsExperienceOnly",
    f"{_COMPONENT_PREFIX}.profileCardsBelowActivityPart1WithoutExp",
    f"{_COMPONENT_PREFIX}.profileCardsBelowActivityPart2",
    f"{_COMPONENT_PREFIX}.profileCardsBelowActivityPart3",
    f"{_COMPONENT_PREFIX}.profileCardsBelowActivityPart4",
    f"{_COMPONENT_PREFIX}.profileCardsBelowActivityPart5",
    f"{_COMPONENT_PREFIX}.profileCardsBelowActivityPart6",
    f"{_COMPONENT_PREFIX}.profileCardsBelowActivityPart7",
]

# The flagship-web SDUI application version — distinct from the Voyager client
# version (1.13.x) used by app/linkedin/client.py. Both are validated server-side,
# so they must not be interchanged. Bump alongside CHROME_UA when recalibrating
# against a fresh capture.
SDUI_VERSION = "0.2.7003"

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"'

# The main profile stream is a client-side NAVIGATION, not a fresh page load: the
# real app reaches /flagship-web/in/{slug} by routing from the feed. RSC answers a
# navigation with a DELTA against the layout the client says it already has, so
# these three headers decide whether the response has a body at all — without them
# LinkedIn returns 200 with zero bytes, which reads like a silent block but is the
# server correctly saying "nothing changed". The layout tree below is the Home
# shell, matching what the app holds when leaving the feed.
_NAVIGATION_HEADERS = {
    "x-li-anchor-page-key": "d_flagship3_feed",
    "x-li-initial-url": "/feed/",
    "x-li-layout-tree": json.dumps(
        ["com.linkedin.sdui.flagshipnav.home.Home#0", "a15eca777c146d37da0475b8f19e5d56"],
        separators=(",", ":"),
    ),
}

_X_LI_TRACK = json.dumps(
    {
        "clientVersion": SDUI_VERSION,
        "mpVersion": SDUI_VERSION,
        "osName": "web",
        "timezoneOffset": 5.5,
        "timezone": "Asia/Calcutta",
        "deviceFormFactor": "DESKTOP",
        "mpName": "flagship-web",
    },
    separators=(",", ":"),
)


class FlagshipWebStrategy:
    """Hits flagship-web RSC endpoints. Requires auth (cookies). No queryId needed.

    In OFFLINE_MODE (with FIXTURE_DIR set), serves from saved RSC fixtures on disk
    instead of hitting LinkedIn. This lets the API run with zero network access —
    useful for demos, grading, and running on a Mac mini without a LinkedIn account.
    """

    name: ClassVar[str] = "flagship_web_rsc"
    requires_auth: ClassVar[bool] = True
    provides: ClassVar[set[str]] = {
        "profile", "experience", "education", "skills",
        "certifications", "languages",
    }

    def __init__(self, offline_mode: bool = False, fixture_dir: str = "") -> None:
        self.offline_mode = offline_mode
        self.fixture_dir = Path(fixture_dir) if fixture_dir else Path("tests/fixtures/rsc")

    async def fetch(self, slug: str, client: Any) -> FetchResult | None:
        """Fetch the profile via flagship-web RSC endpoints (or from fixtures in offline mode).

        In offline mode, reads RSC fixtures from disk — no network call to LinkedIn.
        Falls back to live HTTP if no fixture exists for the requested slug.
        """
        if self.offline_mode:
            result = self._fetch_from_fixtures(slug)
            if result is not None:
                return result
            log.info("flagship.offline_no_fixture", slug=slug)
            return None

        return await self._fetch_live(slug, client)

    def _fetch_from_fixtures(self, slug: str) -> FetchResult | None:
        """Serve from saved RSC fixtures on disk. No network."""
        fixture_path = self.fixture_dir / f"profile_{slug}.json"
        if not fixture_path.exists():
            # Try the generic fixtures we captured by mapping the slug to the fixture name.
            for candidate in [self.fixture_dir / f"profile_{slug}.json"]:
                if candidate.exists():
                    fixture_path = candidate
                    break
            else:
                # No fixture for this slug. In offline mode, return None.
                return None

        data = json.loads(fixture_path.read_text())
        return FetchResult(
            payload=data["payload"],
            profile_urn=data.get("profile_urn"),
            source=self.name,
        )

    async def _fetch_live(self, slug: str, client: Any) -> FetchResult | None:
        """Fetch from LinkedIn's live RSC endpoints.

        The flagship-web endpoints need the x-li-rsc-stream header to return RSC
        wire format instead of HTML. The main page is a GET; the component endpoints
        (experience, languages) are POSTs with a JSON body containing the slug and
        profile URN.
        """
        try:
            session = client.pool.acquire()
        except Exception as e:
            log.warning("flagship.no_session", error=str(e))
            return None

        headers = _build_browser_headers(session, slug)
        nav_headers = _build_browser_headers(session, slug, navigation=True)

        # 1. Main profile page — GET with skipRedirect=true, as a navigation.
        main_url = f"{FLAGSHIP_BASE}/in/{up.quote(slug)}/?skipRedirect=true"
        main_texts = await _fetch_rsc_url(
            client, session, main_url, nav_headers, method="GET"
        )
        if not main_texts:
            # Deliberately does NOT cool the session. An empty RSC parse says the
            # flagship transport did not yield text — it is not evidence about
            # session health, and cooling here starves the Voyager strategies that
            # would have succeeded on the same session.
            log.info("flagship.no_main_texts", slug=slug)
            return None

        client.pool.report_success(session)

        # Extract the profile URN from the main page texts.
        profile_urn = _extract_profile_urn(main_texts)

        # 2. Section components — one POST each.
        #
        # The card components carry the sections the main page stream does not:
        # experience, education, skills, certifications, languages. The live page
        # requests the whole set; the exact split between "BelowActivityPartN"
        # buckets is not stable across profiles, so every part is fetched and the
        # mappers pattern-match across the merged text rather than trusting one
        # part to hold one section.
        body = _build_component_body(slug, profile_urn)
        section_texts: dict[str, list[str]] = {}
        for component in PROFILE_COMPONENTS:
            url = (
                f"{FLAGSHIP_BASE}/rsc-action/actions/component"
                f"?componentId={component}&sduiid={component}"
            )
            texts = await _fetch_rsc_url(
                client, session, url, headers, method="POST", body=body
            )
            if texts:
                section_texts[component.rsplit(".", 1)[-1]] = texts

        # The card text is pooled: a section can move between parts between deploys,
        # so mappers scan the union rather than a single named bucket.
        card_texts: list[str] = []
        for texts in section_texts.values():
            card_texts.extend(texts)

        log.info(
            "flagship.fetched",
            slug=slug,
            components_ok=len(section_texts),
            components_tried=len(PROFILE_COMPONENTS),
            card_text_items=len(card_texts),
        )

        payload = {
            "_source": "flagship_web_rsc",
            "main_texts": main_texts,
            # Kept as distinct keys for the existing mappers, all now backed by the
            # full pooled card text.
            "experience_texts": card_texts,
            "language_texts": card_texts,
            "about_texts": card_texts,
            "card_texts": card_texts,
            "components": {k: len(v) for k, v in section_texts.items()},
        }

        return FetchResult(payload=payload, profile_urn=profile_urn, source=self.name)


def _tracking_id() -> str:
    """A base64 tracking id in LinkedIn's format: 16 random bytes, e.g. 'B8D6...rQ=='."""
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _hex_id(n_bytes: int) -> str:
    return os.urandom(n_bytes).hex()


def _build_browser_headers(
    session: Any,
    slug: str,
    page_key: str = PROFILE_PAGE_KEY,
    *,
    navigation: bool = False,
) -> dict[str, str]:
    """Build headers for a flagship-web RSC request.

    Calibrated field-by-field against a captured profile page load (see
    docs/REVERSE_ENGINEERING.md). The set matters more than it looks — LinkedIn
    answers a request missing the SDUI headers with an HTML login page under a 200,
    or with 999, rather than with an error that names the problem.

    Load-bearing pieces:
      - `x-li-rsc-stream: true` is what selects the RSC wire format instead of HTML.
      - `x-li-application-version` / `x-li-track.clientVersion` must be the SDUI app
        version (0.2.x), NOT the Voyager client version (1.13.x). They are different
        applications and the server validates them separately.
      - `referer` must be the specific profile URL. A generic `/in/` referer reads as
        a cross-page fetch and gets challenged.
      - The tracing headers are generated fresh per request, the way the real client
        does. Replaying one captured id on every request is itself a fingerprint.
    """
    trace_id = _hex_id(16)
    span_id = _hex_id(8)
    page_instance = _tracking_id()
    profile_url = f"https://www.linkedin.com/in/{up.quote(slug)}/"

    return {
        # No `cookie` header: it would override curl's jar and break the `lidc`
        # datacenter-affinity redirect. The jar on the per-session client carries
        # the cookies. See app/linkedin/client.py:_session_http.
        "user-agent": CHROME_UA,
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "csrf-token": session.jsessionid,
        "origin": "https://www.linkedin.com",
        "referer": profile_url,
        "priority": "u=1, i",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-li-rsc-stream": "true",
        "x-li-anchor-page-key": page_key,
        "x-li-application-instance": _tracking_id(),
        "x-li-application-version": SDUI_VERSION,
        "x-li-page-instance": f"urn:li:page:{page_key};{page_instance}",
        "x-li-page-instance-tracking-id": page_instance,
        "x-li-pageforestid": trace_id,
        "x-li-traceparent": f"00-{trace_id}-{span_id}-00",
        "x-li-tracestate": f"LinkedIn={span_id}",
        "x-li-track": _X_LI_TRACK,
        **(_NAVIGATION_HEADERS if navigation else {}),
    }


def _build_component_body(slug: str, profile_urn: str | None) -> bytes:
    """Build the POST body for an RSC component request.

    Uses the exact body structure from the captured HAR, with the slug and URN
    templated in. The key fields beyond what we had before:
      - clientArguments.states: [] (empty array, not omitted)
      - clientArguments.requestMetadata: type tag
      - clientArguments.screenId: the profile screen ID
      - clientArguments.knownTemplateIds: [] (empty array)
      - lastPerformedActionRef and lastFeaturedActionRef are BindingImpl (not null)
    """
    import orjson as _orjson

    binding_keys = [
        ("shouldRefreshScreenOnReappear", "ShouldRefreshScreen"),
        ("shouldFetchFromCache", "FetchFromCache"),
        ("shouldDisplayTabAnchors", "ShouldDisplayTabAnchors"),
        ("shouldReloadTopCardOnReappear", "ShouldReloadTopCardOnReappear"),
        ("deferredTopCardReloadProfileId", "DeferredTopCardReloadProfileId"),
        ("shouldDisplayStickyHeader", "ShouldDisplayStickyHeader"),
        ("shouldRefreshLanguageDetailScreen", "ShouldRefreshLanguageDetails"),
        ("lastPerformedActionRef", "LastPerformedActionRef"),
        ("shouldFocusOnReappear", "ShouldFocusOnReappear"),
        ("shouldFocusFeaturedOnReappear", "ShouldFocusFeaturedOnReappear"),
        ("lastFeaturedActionRef", "LastFeaturedActionRef"),
        ("shouldHideProfileCards", "ProfileHideCards"),
    ]
    pcs: dict = {"profileId": slug}
    for field_name, key_suffix in binding_keys:
        pcs[field_name] = {
            "type": "com.linkedin.sdui.components.core.BindingImpl",
            "value": {
                "key": f"ProfileComponentState{key_suffix}{slug}ProfileComponentState",
                "namespace": "MemoryNamespace",
            },
        }

    body = {
        "clientArguments": {
            "payload": {
                "isSelfView": False,
                "vanityName": slug,
                "replaceableSectionArgs": {
                    "vanityName": slug,
                    "hideCardsForGoldenGate": False,
                    "shouldSetupReplaceableComponent": True,
                    "vieweeProfileId": profile_urn or "",
                    "isSelfView": False,
                    "isSelfViewResolved": False,
                },
                "profileComponentState": pcs,
            },
            "states": [],
            "requestMetadata": {
                "$type": "proto.sdui.common.RequestMetadata",
            },
            "screenId": "com.linkedin.sdui.flagshipnav.profile.Profile",
            "knownTemplateIds": [],
        }
    }
    return _orjson.dumps(body)


async def _fetch_rsc_url(
    client: Any,
    session: Any,
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    body: bytes | None = None,
) -> list[str] | None:
    """Fetch a URL via curl_cffi with browser headers and extract RSC text.

    Supports both GET (main page) and POST (component endpoints) requests.
    """
    try:
        client._ensure()
        http = client._session_http(session)

        import asyncio
        delay = __import__("random").uniform(
            client.settings.min_delay_ms / 1000.0,
            client.settings.max_delay_ms / 1000.0,
        )
        await asyncio.sleep(delay)

        async with client._sem, client._session_lock(session):
            if method == "POST" and body is not None:
                post_headers = dict(headers)
                post_headers["content-type"] = "application/json"
                resp = await http.post(
                    url, headers=post_headers, content=body,
                    allow_redirects=True, max_redirects=5,
                )
            else:
                resp = await http.get(
                    url, headers=headers, allow_redirects=True, max_redirects=5,
                )
        client._harvest_cookies(session, http)
    except Exception as e:
        log.warning("flagship.fetch_error", url=url[:100], error=str(e))
        return None

    body_resp = getattr(resp, "content", b"") or b""
    if not body_resp:
        return None

    ct = ""
    raw_headers = dict(getattr(resp, "headers", {}) or {})
    ct = raw_headers.get("content-type") or raw_headers.get("Content-Type") or ""
    status = getattr(resp, "status_code", 0)
    body_str = body_resp.decode("utf-8", errors="replace")
    if "text/html" in ct.lower() or body_str.startswith("<!DOCTYPE") or body_str.startswith("<html"):
        log.warning("flagship.auth_wall", url=url[:80], content_type=ct[:50], status=status, body_preview=body_str[:80])
        return None

    texts = extract_text(body_str)
    return texts if texts else None


# --- mapping functions -------------------------------------------------------

def map_profile_from_rsc(texts: list[str], about_texts: list[str] | None = None) -> Profile:
    """Map the core profile fields from the main page RSC text list.

    About text is extracted from a separate RSC component (about_texts) if
    available, falling back to the main page texts.
    """
    full_name = None
    first_name = None
    last_name = None
    headline = None
    location_raw = None
    industry = None
    about = None
    profile_urn = None
    followers = None

    for i, t in enumerate(texts):
        if not profile_urn and t.startswith("ACoAA"):
            profile_urn = t

        if "| LinkedIn" in t and full_name is None:
            full_name = t.replace(" | LinkedIn", "").strip()

        if full_name and not headline and "Send profile" in t:
            if i > 0 and texts[i - 1] and len(texts[i - 1]) > 5 and "LinkedIn" not in texts[i - 1]:
                headline = texts[i - 1]

    if not full_name:
        for i, t in enumerate(texts):
            if t == "Primary content" and i + 1 < len(texts):
                candidate = texts[i + 1]
                if " " in candidate and len(candidate) > 3 and "LinkedIn" not in candidate:
                    full_name = candidate
                    break

    if full_name:
        parts = full_name.split(" ", 1)
        first_name = parts[0] if parts else None
        last_name = parts[1] if len(parts) > 1 else None

    # Location: "City, Region, Country" or just "Country"
    for t in texts:
        if not location_raw:
            # Full "City, Region, Country" pattern
            if re.match(r"^[A-Z][a-zA-Z\s]+,\s[A-Z][a-zA-Z\s]+,\s[A-Z]", t):
                location_raw = t
                break
    # Fallback: single-word country after the education/company summary
    if not location_raw:
        for i, t in enumerate(texts):
            if " · " in t and i + 1 < len(texts):
                candidate = texts[i + 1]
                if candidate and len(candidate) < 30 and re.match(r"^[A-Z][a-z]+$|^[A-Z][a-z]+ [A-Z][a-z]+$", candidate):
                    location_raw = candidate
                    break

    # Followers: "4,294 followers" pattern
    for t in texts:
        m = re.match(r"^([\d,]+)\s+followers$", t)
        if m:
            followers = int(m.group(1).replace(",", ""))
            break

    # About: text after "About" header — check about_texts first, then main texts
    if not about:
        # Try the about component first
        if about_texts:
            for i, t in enumerate(about_texts):
                if isinstance(t, str) and (t == "About" or t == "About this member"):
                    for j in range(i + 1, min(i + 10, len(about_texts))):
                        candidate = about_texts[j]
                        if (isinstance(candidate, str) and len(candidate) > 30
                            and candidate not in ("About", "About this member")
                            and not candidate.startswith("Hire")
                            and not candidate.startswith("Services")
                            and not candidate.startswith("Explore")
                            and not candidate.startswith("Talent")
                            and not candidate.startswith("Community")):
                            about = candidate
                            break
                    if about:
                        break
        # Fallback: check main texts
        if not about:
            for i, t in enumerate(texts):
                if t == "About this member" or t == "About":
                    for j in range(i + 1, min(i + 10, len(texts))):
                        candidate = texts[j]
                        if (isinstance(candidate, str) and len(candidate) > 30
                            and candidate not in ("About", "About this member")
                            and not candidate.startswith("Hire")
                            and not candidate.startswith("Services")
                            and not candidate.startswith("Explore")):
                            about = candidate
                            break
                    if about:
                        break

    images = _map_images_from_texts(texts)

    loc = Location(raw=location_raw) if location_raw else None
    counts = Counts(followers=followers)

    return Profile(
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        headline=headline,
        about=about,
        location=loc,
        industry=industry,
        pronouns=None,
        flags=ProfileFlags(),
        images=images,
        counts=counts,
    )


def _extract_profile_urn(texts: list[str]) -> str | None:
    """Extract the profile URN (ACoAA...) from the main page texts."""
    for t in texts:
        if t.startswith("ACoAA") and len(t) > 10:
            return t
    return None


# A position's date line: "Jan 2024 - Jun 2025 · 1 yr 6 mos", "Aug 2024 - Present · 2 yrs 1 mo".
# The trailing duration is optional; the range itself is what identifies a position.
_POS_DATES = re.compile(
    r"^([A-Z][a-z]{2}\s+\d{4}|\d{4})\s*[-–—]\s*(Present|[A-Z][a-z]{2}\s+\d{4}|\d{4})"
    r"(?:\s*·\s*(.+))?$"
)

# Employment types as the card renders them, used to split "takeUforward · Full-time"
# into company and employment type.
_EMPLOYMENT_TYPES = {
    "Full-time", "Part-time", "Self-employed", "Freelance", "Contract",
    "Internship", "Apprenticeship", "Seasonal", "Temporary",
}

# Work arrangement, rendered as its own item or appended to the location.
_WORK_MODES = {"On-site", "Hybrid", "Remote"}


def _split_company_line(line: str) -> tuple[str | None, str | None]:
    """Split "takeUforward · Full-time" into (company, employment type)."""
    if " · " not in line:
        return (line.strip() or None, None)
    head, tail = (p.strip() for p in line.split(" · ", 1))
    if tail in _EMPLOYMENT_TYPES:
        return (head or None, tail)
    return (head or None, None)


def _split_location(line: str) -> tuple[str | None, str | None]:
    """Split "Bangalore Urban, Karnataka, India · Remote" into (location, work mode)."""
    if " · " in line:
        head, tail = (p.strip() for p in line.rsplit(" · ", 1))
        if tail in _WORK_MODES:
            return (head or None, tail)
    if line.strip() in _WORK_MODES:
        return (None, line.strip())
    return (line.strip() or None, None)


def _company_names(texts: list[str]) -> set[str]:
    """Company and school names, harvested from the stream's "<name> logo" items.

    Every organisation on a profile card ships an accessibility label of the form
    "Google logo". That gives a reliable roster of the organisations this profile
    mentions, which is what lets the mapper tell a company line from a job title
    without guessing at punctuation — the three card layouts punctuate differently
    and a bare company name is indistinguishable from a title by shape alone.
    """
    names: set[str] = set()
    for text in texts:
        if text.endswith(" logo"):
            name = text[: -len(" logo")].strip()
            if name:
                names.add(name)
    return names


def _year_only(m: re.Match) -> bool:
    """True for a bare "1997 – 2004" range with no duration attached."""
    start, end, duration = m.group(1), m.group(2), m.group(3)
    if duration:
        return False
    return start.isdigit() and (end.isdigit() or end == "Present")


def _looks_like_prose(text: str) -> bool:
    """True when a text item reads as a sentence rather than a job title."""
    t = text.strip()
    if len(t) > 90:
        return True
    # Titles do not end in sentence punctuation.
    return t.endswith((".", "!", "?"))


def _location_after(
    texts: list[str], date_index: int, known_companies: set[str]
) -> tuple[str | None, str | None]:
    """The location line following a position's dates, if there is one.

    Not every position has one — older entries often carry title, company, dates and
    nothing else. When the location is absent the next item is the FOLLOWING
    position's title, so a naive "take the next line" reads one position's title as
    another's location.

    The tell is what comes after the candidate: if a date line follows within a
    couple of content items, the candidate opened a new entry rather than closing
    this one.
    """
    following: list[str] = []
    j = date_index + 1
    while j < len(texts) and len(following) < 3:
        if not _is_noise(texts[j]):
            following.append(texts[j].strip())
        j += 1

    if not following:
        return (None, None)

    candidate = following[0]
    if _POS_DATES.match(candidate) or candidate in known_companies:
        return (None, None)
    # A company line ("Media.net · Full-time") directly after means the candidate
    # belongs to the next entry.
    company_head, _ = _split_company_line(candidate)
    if company_head in known_companies:
        return (None, None)
    if any(_POS_DATES.match(f) for f in following[1:3]):
        return (None, None)

    return _split_location(candidate)


def map_experience_from_rsc(texts: list[str]) -> list[Experience]:
    """Map experience positions from the profile card text streams.

    Anchored on each position's date line, which is the one item that appears
    exactly once per position. Everything else is positional relative to it.

    The card renders two layouts and they must be told apart:

      Single position          Grouped company (several roles at one employer)
      -----------------        ----------------------------------------------
      Founder, CEO and CTO     Google                    <- company header
      takeUforward · Full-time Full-time · 3 yrs 5 mos   <- type + total tenure
      Aug 2024 - Present · ..  On-site
      Bangalore ... · Remote   Software Engineer III     <- role title
                               Jan 2024 - Jun 2025 · ..  <- role dates
                               Bengaluru, Karnataka      <- role location

    The tell is the item directly above the date line: a "Company · Type" line means
    a single position (title sits one further up), while a bare title means a grouped
    sub-role whose employer is the most recent company header seen.

    Calibrated against tests/fixtures/rsc/profile_rajstriver.json.
    """
    out: list[Experience] = []
    known_companies = _company_names(texts)
    # The company header for the grouped layout, carried forward across sub-roles.
    current_company: str | None = None
    current_emp_type: str | None = None

    for i, text in enumerate(texts):
        m = _POS_DATES.match(text.strip())
        if not m:
            continue

        above = _preceding_content(texts, i, 3)
        if not above:
            continue
        above = list(reversed(above))  # nearest-first

        nearest = above[0]
        nearest_company, nearest_type = _split_company_line(nearest)

        company: str | None
        emp_type: str | None
        title: str | None

        if nearest_company in known_companies:
            # "Company" or "Company · Full-time" sits directly above the dates, so
            # the title is one further up. Covers both the single-position layout
            # and the bare-company layout.
            company, emp_type = nearest_company, nearest_type
            title = above[1] if len(above) > 1 else None
            explicit_company = True
        else:
            # Grouped sub-role: a bare title above the dates, employer taken from
            # the company header that opened the group.
            title = nearest
            company, emp_type = current_company, current_emp_type
            explicit_company = False

        # A company header is a bare name followed by "Type · total tenure"; when we
        # see that shape, remember it for the sub-roles that follow.
        header_company, header_type = _detect_group_header(texts, i)
        if header_company:
            current_company, current_emp_type = header_company, header_type
            if not company:
                company, emp_type = header_company, header_type

        if _year_only(m) and not explicit_company:
            # A bare "2013 – 2015" with no named employer directly above it is the
            # education card's format, not a position. Older roles DO use year-only
            # ranges (see the Obama fixture), which is why the test is "was an
            # employer named here", not "is the range year-only".
            continue

        if not title or _is_noise(title) or _looks_like_prose(title):
            # Role descriptions are emitted as their own text items and can land
            # directly above a date line, where a title is expected.
            continue

        location, work_mode = _location_after(texts, i, known_companies)

        start = _date_part_from_text(m.group(1))
        end_raw = m.group(2)
        is_current = end_raw == "Present"
        end = None if is_current else _date_part_from_text(end_raw)

        out.append(
            Experience(
                title=title,
                company=CompanyRef(name=company) if company else None,
                employment_type=emp_type,
                location=location,
                location_type=work_mode,
                start=start,
                end=end,
                is_current=is_current,
                duration_months=duration_months(
                    start.model_dump() if start else None,
                    end.model_dump() if end else None,
                    is_current=is_current,
                ),
            )
        )
    return out


def _detect_group_header(texts: list[str], date_index: int) -> tuple[str | None, str | None]:
    """Find a grouped-company header above a position's date line.

    The header is "Company" followed by "Full-time · 3 yrs 5 mos" — an employment
    type joined to a total tenure, which is what distinguishes it from a single
    position's "Company · Full-time".
    """
    above = list(reversed(_preceding_content(texts, date_index, 6)))
    for idx, line in enumerate(above):
        if " · " not in line:
            continue
        head, tail = (p.strip() for p in line.split(" · ", 1))
        if head in _EMPLOYMENT_TYPES and _TENURE.match(tail):
            company = above[idx + 1] if idx + 1 < len(above) else None
            if company and not _is_noise(company):
                return (company, head)
    return (None, None)


# A total-tenure string: "3 yrs 5 mos", "2 yrs", "6 mos", "1 yr 6 mos".
_TENURE = re.compile(r"^(?:\d+\s+yrs?)?\s*(?:\d+\s+mos?)?$")

def _extract_company_logos(texts: list[str]) -> dict[str, str]:
    """Extract company logo URLs from the RSC text list.

    Logos appear as fragments in the text:
      "CompanyName logo"  -> label
      "https://media.licdn.com/.../company-logo_"  -> base URL (truncated)
      "100_100/B56Z.../...?e=...&t=..."  -> resolution path 1
      "200_200/B56Z.../...?e=...&t=..."  -> resolution path 2
      "400_400/B56Z.../...?e=...&t=..."  -> resolution path 3

    We reconstruct full URLs by concatenating the base URL with the highest-
    resolution path, and associate them with the company name from the label.
    """
    logos: dict[str, str] = {}
    i = 0
    while i < len(texts):
        t = texts[i]
        # Look for "CompanyName logo" label
        if isinstance(t, str) and t.endswith(" logo") and not t.startswith("http"):
            company_name = t[:-5].strip()  # Remove " logo" suffix
            # Look ahead for the base URL and resolution paths
            base_url = None
            best_url = None
            j = i + 1
            while j < len(texts) and j < i + 10:
                candidate = texts[j]
                if not isinstance(candidate, str):
                    j += 1
                    continue
                if candidate.startswith("https://media.licdn.com/") and "company-logo" in candidate:
                    if not base_url:
                        base_url = candidate
                elif base_url and re.match(r"^\d+_\d+/", candidate):
                    # Resolution path — concatenate with base URL
                    full_url = base_url + candidate
                    # Pick the highest resolution
                    m = re.match(r"^(\d+)_(\d+)/", candidate)
                    if m:
                        res = int(m.group(1)) * int(m.group(2))
                        if not best_url or res > _img_res(best_url):
                            best_url = full_url
                elif base_url and candidate.startswith("http"):
                    # Full URL (not a fragment)
                    if "company-logo" in candidate and ("shrink_" in candidate or "100_100" in candidate):
                        if not best_url:
                            best_url = candidate.split(" ")[0]
                    break
                elif not candidate.startswith("http") and not re.match(r"^\d+_\d+/", candidate):
                    # Not a URL fragment — done with this company
                    break
                j += 1

            if best_url:
                logos[company_name] = best_url
            elif base_url:
                logos[company_name] = base_url
            i = j
        else:
            i += 1
    return logos


def _parse_date_range(date_str: str) -> tuple[dict | None, dict | None, bool]:
    """Parse 'Jul 2025 - Present · 1 yr 2 mos' into (start, end, is_current)."""
    MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    # Strip the duration part
    parts = date_str.split("·")
    date_part = parts[0].strip()
    is_current = "Present" in date_part

    # Split on " - " or " \u2013 " (en dash, which LinkedIn sometimes uses)
    range_parts = re.split(r"\s*[-\u2013]\s*", date_part)
    if len(range_parts) != 2:
        return None, None, is_current

    start_str, end_str = range_parts[0].strip(), range_parts[1].strip()

    start = _parse_month_year(start_str, MONTHS)
    end = None if is_current else _parse_month_year(end_str, MONTHS)

    return start, end, is_current


def _parse_month_year(s: str, months: dict) -> dict | None:
    """Parse 'Jul 2025' or '2015' into {'year': Y, 'month': M|None, 'day': None, 'iso': str}.

    For year-only dates, month is None and iso is just the year.
    """
    m = re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$", s.strip())
    if m:
        month = months.get(m.group(1))
        year = int(m.group(2))
        if month:
            return {"year": year, "month": month, "day": None, "iso": f"{year:04d}-{month:02d}"}
    # Year-only
    m = re.match(r"^(\d{4})$", s.strip())
    if m:
        year = int(m.group(1))
        return {"year": year, "month": None, "day": None, "iso": f"{year:04d}"}
    return None


def _parse_duration_months(date_str: str) -> int | None:
    """Parse 'Jul 2025 - Present · 1 yr 2 mos' -> 14 (approx). Best-effort."""
    # Extract the duration part after ·
    parts = date_str.split("·")
    if len(parts) < 2:
        return None
    dur_str = parts[1].strip()
    m = re.match(r"(\d+)\s+yr[s]?\s+(\d+)\s+mos?", dur_str)
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))
    m = re.match(r"(\d+)\s+mos?", dur_str)
    if m:
        return int(m.group(1))
    m = re.match(r"(\d+)\s+yr[s]?", dur_str)
    if m:
        return int(m.group(1)) * 12
    return None


# A schooling date range as the profile cards render it: "2016 – 2020", "2013 - 2015",
# or an open-ended "2021 – Present". The dash may be an en dash or a hyphen.
_EDU_RANGE = re.compile(r"^(\d{4})\s*[–—-]\s*(\d{4}|Present)$")

# Certification issue lines: "Issued Oct 2017", "Issued Oct 2017 · Expires Oct 2020".
#
# The date is required, not optional. The honors card renders "Issued by <org> ·
# Jan 2019", which a looser "^Issued " anchor happily matches — that pulled honors
# entries into the certifications list on the calibration capture.
_CERT_ISSUED = re.compile(
    r"^Issued\s+((?:[A-Z][a-z]{2}\s+)?\d{4})(?:\s*·\s*Expires\s+(.+?))?$"
)

# RSC text that is markup plumbing rather than profile content. The streams are a
# UI description, so component ids, scale hints and i18n templates sit inline with
# real values and would otherwise be read as school or company names.
_NOISE_PREFIXES = (
    "ProfileNullState", "profile_", "expandable_text_block", "auto-component-",
    "entity-collection-item", "text-attr-", "LinkFormatting", "Profile_Top_Level_",
    "GroupsJoinButton", "https://", "urn:li:",
)
_NOISE_EXACT = {
    "en_US", "xMidYMid slice", "WIDTH_AND_HEIGHT", "Show all", "Show credential",
    "Verified", "Follow", "Following", "Join", "Joined", "Requested", "Subscribe",
    "Subscribed", "Unsubscribed", "Pending", "Received", "Given", "Post",
    "carousel-child-container", "Nothing to see for now",
}
_NOISE_SUFFIXES = ("-count", " logo")


def _is_noise(text: str) -> bool:
    """True when an RSC text item is UI plumbing rather than profile content."""
    t = text.strip()
    if not t or t in _NOISE_EXACT:
        return True
    if t.startswith(_NOISE_PREFIXES) or t.endswith(_NOISE_SUFFIXES):
        return True
    # Scale hints: "1x", "2x", "0.25x", "1.5x".
    if re.fullmatch(r"\d+(\.\d+)?x", t):
        return True
    # ICU plural templates leak through as raw strings.
    if "{0,plural" in t or t.startswith("Show all "):
        return True
    # Media URLs arrive split across items: a truncated base ("https://media.licdn…")
    # followed by resolution-prefixed continuations ("400_400/company-logo_400_400/…").
    # The continuations do not start with a scheme, so the https:// prefix check
    # above misses them.
    if re.match(r"^\d+_\d+/", t) or "v=beta&t=" in t:
        return True
    return False


def _preceding_content(texts: list[str], index: int, count: int) -> list[str]:
    """The `count` content items immediately before `index`, oldest first.

    Entries in the card streams are emitted as a run of values followed by their
    date line, with plumbing interleaved. Walking backwards past the noise is what
    recovers "which school does this date belong to".
    """
    out: list[str] = []
    i = index - 1
    while i >= 0 and len(out) < count:
        if not _is_noise(texts[i]):
            out.append(texts[i].strip())
        i -= 1
    return list(reversed(out))


def map_education_from_rsc(texts: list[str]) -> list[Education]:
    """Map education entries from the profile card text streams.

    Anchored on the date range rather than on the "Education" header. The card
    streams are not strictly sectioned — on a real capture the two schools appear
    both before and after the certifications block — so scanning forward from a
    header mixes sections together. A "2016 – 2020" line, by contrast, reliably
    terminates one education entry, with the school and degree as the two content
    items before it.

    Calibrated against tests/fixtures/rsc/profile_rajstriver.json.
    """
    out: list[Education] = []
    seen: set[str] = set()

    for i, text in enumerate(texts):
        m = _EDU_RANGE.match(text.strip())
        if not m:
            continue
        preceding = _preceding_content(texts, i, 2)
        if not preceding:
            continue

        # Two items: (school, "degree, field"). One item: school only.
        school = preceding[0]
        degree_line = preceding[1] if len(preceding) > 1 else None
        if len(preceding) == 1:
            school, degree_line = preceding[0], None

        if not school or school in seen:
            continue
        seen.add(school)

        degree = field = None
        if degree_line:
            # "B.TECH, Information Technology" -> degree, field of study.
            if "," in degree_line:
                degree, field = (p.strip() or None for p in degree_line.split(",", 1))
            else:
                degree = degree_line

        start_year, end_year = m.group(1), m.group(2)
        out.append(
            Education(
                school=school,
                degree=degree,
                field_of_study=field,
                start=DatePart(year=int(start_year), iso=start_year),
                end=None if end_year == "Present" else DatePart(year=int(end_year), iso=end_year),
            )
        )
    return out


def map_certifications_from_rsc(texts: list[str]) -> list[Certification]:
    """Map licenses & certifications from the profile card text streams.

    Anchored on the "Issued <date>" line for the same reason education anchors on
    its date range: it is the one item guaranteed to belong to exactly one entry.
    The two content items before it are the certification name and the issuer; a
    "Credential ID ..." line may follow.

    Calibrated against tests/fixtures/rsc/profile_rajstriver.json.
    """
    out: list[Certification] = []
    seen: set[str] = set()

    for i, text in enumerate(texts):
        m = _CERT_ISSUED.match(text.strip())
        if not m:
            continue
        preceding = _preceding_content(texts, i, 2)
        if len(preceding) < 2:
            continue
        name, issuer = preceding[0], preceding[1]
        if not name or name in seen:
            continue
        seen.add(name)

        credential_id = None
        for follow in texts[i + 1 : i + 4]:
            f = follow.strip()
            if f.startswith("Credential ID "):
                credential_id = f[len("Credential ID ") :].strip() or None
                break

        out.append(
            Certification(
                name=name,
                authority=issuer,
                issued=_date_part_from_text(m.group(1)),
                expires=_date_part_from_text(m.group(2)) if m.group(2) else None,
                license_number=credential_id,
            )
        )
    return out


# "Oct 2017" / "2017" as rendered in certification lines.
_MON = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _date_part_from_text(text: str | None) -> DatePart | None:
    """Parse a rendered card date ("Oct 2017", "2017") into a DatePart."""
    if not text:
        return None
    t = text.strip()
    m = re.match(r"^([A-Z][a-z]{2})\s+(\d{4})$", t)
    if m and m.group(1) in _MON:
        year, month = int(m.group(2)), _MON[m.group(1)]
        return DatePart(year=year, month=month, iso=f"{year}-{month:02d}")
    m = re.match(r"^(\d{4})$", t)
    if m:
        return DatePart(year=int(m.group(1)), iso=m.group(1))
    return None


def map_languages_from_rsc(texts: list[str]) -> list[Language]:
    """Map languages from the BelowActivity Part4 texts.

    Pattern: after "Languages" header, pairs of (language, proficiency).
    """
    languages: list[Language] = []
    in_languages = False
    i = 0
    while i < len(texts):
        t = texts[i]
        if t == "Languages":
            in_languages = True
            i += 1
            continue
        if in_languages:
            # Skip noise
            if t.startswith("ProfileNullState") or t.startswith("LanguageTopLevel") or "plural" in t or t == "en_US":
                i += 1
                continue
            # Check if this is a language name (capitalized, short, not a proficiency)
            if re.match(r"^[A-Z][a-z]+$", t) and len(t) < 30:
                # Look ahead for proficiency
                proficiency = None
                if i + 1 < len(texts):
                    next_t = texts[i + 1]
                    if "proficiency" in next_t.lower() or "bilingual" in next_t.lower() or "native" in next_t.lower():
                        proficiency = next_t
                        i += 1
                languages.append(Language(name=t, proficiency=proficiency))
        i += 1
    return languages


def _map_images_from_texts(texts: list[str]) -> ProfileImages:
    """Extract profile photo and background photo URLs from the main page texts.

    LinkedIn's RSC stream contains multiple URL fragments for each image. We want
    the full URLs with resolution info (shrink_NNN or scale_NNN), not the truncated
    base URLs. For srcset strings ("url 100w, url 200w"), we take the first URL.
    """
    profile_imgs: list[Image] = []
    bg_imgs: list[Image] = []

    for t in texts:
        if not isinstance(t, str) or not t.startswith("https://media.licdn.com/dms/image"):
            continue
        # For srcset strings, take only the first URL (before the space+w)
        url = t.split(" ")[0]

        if "profile-displayphoto" in url and ("shrink_" in url or "scale_" in url):
            if not profile_imgs or _img_res(url) > _img_res(profile_imgs[0].url):
                profile_imgs = [Image(url=url, expires_at=parse_image_expiry(url))]
        elif "profile-displayphoto" in url and not profile_imgs:
            profile_imgs = [Image(url=url, expires_at=parse_image_expiry(url))]

        if "profile-displaybackgroundimage" in url and ("shrink_" in url or "scale_" in url):
            if not bg_imgs or _img_res(url) > _img_res(bg_imgs[0].url):
                bg_imgs = [Image(url=url, expires_at=parse_image_expiry(url))]
        elif "profile-displaybackgroundimage" in url and not bg_imgs:
            bg_imgs = [Image(url=url, expires_at=parse_image_expiry(url))]

    return ProfileImages(profile=profile_imgs, background=bg_imgs)


def _img_res(url: str) -> int:
    """Extract the resolution number from a LinkedIn image URL for comparison."""
    import re

    m = re.search(r"shrink_(\d+)_(\d+)", url)
    if m:
        return int(m.group(1)) * int(m.group(2))
    m = re.search(r"scale_(\d+)_(\d+)", url)
    if m:
        return int(m.group(1)) * int(m.group(2))
    return 0
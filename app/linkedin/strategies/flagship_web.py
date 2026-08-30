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

import json
import re
import urllib.parse as up
from pathlib import Path
from typing import Any, ClassVar

import structlog

from app.linkedin.rsc_parser import extract_text
from app.linkedin.strategies import FetchResult
from app.models import (
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
from app.normalize.images import parse_image_expiry

log = structlog.get_logger("strategy.flagship_web")

FLAGSHIP_BASE = "https://www.linkedin.com/flagship-web"


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

        headers = _build_browser_headers(session)

        # 1. Main profile page — GET with skipRedirect=true
        main_url = f"{FLAGSHIP_BASE}/in/{up.quote(slug)}/?skipRedirect=true"
        main_texts = await _fetch_rsc_url(client, main_url, headers, method="GET")
        if not main_texts:
            client.pool.report_failure(session, hard=True)
            return None

        client.pool.report_success(session)

        # Extract the profile URN from the main page texts.
        profile_urn = _extract_profile_urn(main_texts)

        # 2. Experience section — POST to the component endpoint.
        # If the POST fails (LinkedIn's RSC protocol is strict), fall back to
        # parsing the experience from the main page texts, which contain the
        # topcard summary ("Company · School") and sometimes position titles.
        exp_body = _build_component_body(slug, profile_urn)
        exp_url = (
            f"{FLAGSHIP_BASE}/rsc-action/actions/component"
            f"?componentId=com.linkedin.sdui.generated.profile.dsl.impl.profileCardsExperienceOnly"
            f"&sduiid=com.linkedin.sdui.generated.profile.dsl.impl.profileCardsExperienceOnly"
        )
        exp_texts = await _fetch_rsc_url(client, exp_url, headers, method="POST", body=exp_body)

        # 3. Languages section — POST
        lang_url = (
            f"{FLAGSHIP_BASE}/rsc-action/actions/component"
            f"?componentId=com.linkedin.sdui.generated.profile.dsl.impl.profileCardsBelowActivityPart4"
            f"&sduiid=com.linkedin.sdui.generated.profile.dsl.impl.profileCardsBelowActivityPart4"
        )
        lang_texts = await _fetch_rsc_url(client, lang_url, headers, method="POST", body=exp_body)

        # 4. If component POSTs failed, try the public HTML page for more data.
        # The standard /in/{slug}/ URL returns HTML with JSON-LD that has
        # name, headline, and sometimes experience/education.
        if not exp_texts:
            log.info("flagship.component_post_failed_trying_html", slug=slug)
            html_url = f"https://www.linkedin.com/in/{up.quote(slug)}/"
            html_texts = await _fetch_rsc_url(client, html_url, headers, method="GET")
            if html_texts:
                # Merge any experience-relevant texts from the HTML page.
                exp_texts = html_texts

        payload = {
            "_source": "flagship_web_rsc",
            "main_texts": main_texts,
            "experience_texts": exp_texts or [],
            "language_texts": lang_texts or [],
        }

        return FetchResult(payload=payload, profile_urn=profile_urn, source=self.name)


def _build_browser_headers(session: Any) -> dict[str, str]:
    """Build headers for flagship-web RSC requests.

    The critical header is `x-li-rsc-stream: true` — that's what tells LinkedIn to
    return the RSC wire format (application/octet-stream) instead of the full HTML
    page. Without it, you get HTML back regardless of the accept header.

    Also needs:
      - csrf-token: the JSESSIONID value (quotes stripped, same as Voyager)
      - accept: */*
      - sec-fetch-mode: cors, sec-fetch-dest: empty (client-side fetch, not page load)
    """
    return {
        "cookie": f'li_at={session.li_at}; JSESSIONID="{session.jsessionid}"',
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "csrf-token": session.jsessionid,
        "origin": "https://www.linkedin.com",
        "referer": "https://www.linkedin.com/in/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-li-rsc-stream": "true",
        "x-li-anchor-page-key": "d_flagship3_profile_view_base",
        "x-li-track": '{"clientVersion":"0.2.7003","mpVersion":"0.2.7003","osName":"web","timezoneOffset":5.5,"timezone":"Asia/Calcutta","deviceFormFactor":"DESKTOP","mpName":"d_flagship3"}',
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

        if client._http is None:
            from curl_cffi.requests import AsyncSession

            client._http = AsyncSession(impersonate=client.settings.impersonate)

        import asyncio
        delay = __import__("random").uniform(
            client.settings.min_delay_ms / 1000.0,
            client.settings.max_delay_ms / 1000.0,
        )
        await asyncio.sleep(delay)

        async with client._sem:
            if method == "POST" and body is not None:
                post_headers = dict(headers)
                post_headers["content-type"] = "application/json"
                resp = await client._http.post(url, headers=post_headers, content=body, allow_redirects=True)
            else:
                resp = await client._http.get(url, headers=headers, allow_redirects=True)
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

def map_profile_from_rsc(texts: list[str]) -> Profile:
    """Map the core profile fields from the main page RSC text list."""
    # Name: the first occurrence of a "First Last" pattern after "Primary content"
    # or the title "Name | LinkedIn"
    full_name = None
    first_name = None
    last_name = None
    headline = None
    location_raw = None
    industry = None
    about = None
    profile_urn = None

    for i, t in enumerate(texts):
        # Profile URN
        if not profile_urn and t.startswith("ACoAA"):
            profile_urn = t

        # Title tag: "Name | LinkedIn"
        if "| LinkedIn" in t and full_name is None:
            full_name = t.replace(" | LinkedIn", "").strip()

        # Headline: appears after the name in the topcard, often before "Send profile"
        if full_name and not headline and "Send profile" in t:
            # The headline is the text just before "Send profile in a message"
            if i > 0 and texts[i - 1] and len(texts[i - 1]) > 5 and "LinkedIn" not in texts[i - 1]:
                headline = texts[i - 1]

    # Fallback: look for name after "Primary content"
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

    # Location: look for a pattern like "City, Region, Country"
    for t in texts:
        if not location_raw and re.match(r"^[A-Z][a-zA-Z\s]+,\s[A-Z][a-zA-Z\s]+,\s[A-Z]", t):
            location_raw = t
            break

    # Education + company summary: "Company · School" pattern
    education_school = None
    for t in texts:
        if " · " in t and not education_school:
            parts = t.split(" · ")
            if len(parts) == 2:
                education_school = parts[1].strip()

    # Images: find profile photo and cover photo URLs
    images = _map_images_from_texts(texts)

    loc = Location(raw=location_raw) if location_raw else None

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
        counts=Counts(),
    )


def _extract_profile_urn(texts: list[str]) -> str | None:
    """Extract the profile URN (ACoAA...) from the main page texts."""
    for t in texts:
        if t.startswith("ACoAA") and len(t) > 10:
            return t
    return None


def map_experience_from_rsc(texts: list[str]) -> list[Experience]:
    """Map experience positions from the experience section RSC text list.

    The texts are in document order. The date_range is the anchor — we find it
    with a regex and walk backwards to get the company and title.

    LinkedIn uses two text orderings observed in captures:
      Layout A: company, duration, location, title, emp_type, date
      Layout B: title, company, date

    Both share the invariant that the date is the anchor, and the two texts
    immediately before it are (company, title) in some order. We heuristically
    detect which is which: if one of them is a known employment type, it's the
    emp_type (Layout A); otherwise the one closer to the date is the company
    (Layout B) and the one further is the title (Layout A: reversed).
    """
    DATE_RE = re.compile(
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*[-\u2013]\s*"
        r"(?:Present|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4})"
        r"(?:\s*[·•]\s*(\d+\s+yr[s]?\s+\d+\s+mos?|\d+\s+mos?|\d+\s+yr[s]?))?"
    )
    YEAR_RANGE_RE = re.compile(r"^\d{4}\s*[-\u2013]\s*\d{4}$")
    DURATION_RE = re.compile(r"^(\d+\s+yr[s]?\s+\d+\s+mos?|\d+\s+mos?|\d+\s+yr[s]?)$")
    EMPLOYMENT_TYPES = {"Full-time", "Part-time", "Internship", "Contract", "Freelance", "Self-employed"}
    LOCATION_TYPES = {"On-site", "Hybrid", "Remote"}
    LOCATION_RE = re.compile(r"^[A-Z][a-zA-Z\s]+,\s[A-Z][a-zA-Z\s]+")
    SKILLS_RE = re.compile(r".*\+\d+ skills$")

    positions: list[Experience] = []
    i = 0
    while i < len(texts):
        t = texts[i]
        date_match = DATE_RE.match(t)
        if not date_match:
            # Also match year-only ranges: "1997 \u2013 2004"
            year_match = YEAR_RANGE_RE.match(t)
            if year_match:
                date_match = year_match
        if date_match:
            title = None
            company_name = None
            employment_type = None
            location = None
            location_type = None
            date_str = t

            # Walk backwards from the date. The texts before it are, in reverse
            # order: either [company, title, ...] (Layout B) or
            # [emp_type, title, location, duration, company, ...] (Layout A).
            j = i - 1

            # First, check for the "Company · Type" composite (e.g. "BDO · Internship").
            if j >= 0 and " · " in (texts[j] or ""):
                parts = texts[j].split(" · ")
                if len(parts) == 2 and parts[1] in EMPLOYMENT_TYPES:
                    company_name = parts[0].strip()
                    employment_type = parts[1].strip()
                    j -= 1
                    if j >= 0 and not DATE_RE.match(texts[j] or "") and texts[j] not in LOCATION_TYPES:
                        title = texts[j]
                        j -= 1
            else:
                # The text immediately before the date is either:
                # - the company name (Layout B: title, company, date)
                # - the employment_type (Layout A: ..., title, emp_type, date)
                if j >= 0 and texts[j] in EMPLOYMENT_TYPES:
                    # Layout A: emp_type, then title, then maybe location, then company
                    employment_type = texts[j]
                    j -= 1
                    if j >= 0 and not DATE_RE.match(texts[j] or "") and texts[j] not in LOCATION_TYPES:
                        title = texts[j]
                        j -= 1
                    if j >= 0 and LOCATION_RE.match(texts[j] or ""):
                        location = texts[j]
                        j -= 1
                    while j >= 0 and (DURATION_RE.match(texts[j] or "") or SKILLS_RE.match(texts[j] or "")):
                        j -= 1
                    if j >= 0 and not DATE_RE.match(texts[j] or "") and texts[j] not in LOCATION_TYPES:
                        candidate = texts[j]
                        if candidate and not candidate.endswith(" logo") and not candidate.startswith("http"):
                            company_name = candidate
                else:
                    # Layout B: title, company, date
                    # texts[j] = company, texts[j-1] = title
                    if j >= 0 and not DATE_RE.match(texts[j] or "") and texts[j] not in LOCATION_TYPES:
                        company_name = texts[j]
                        j -= 1
                    if j >= 0 and not DATE_RE.match(texts[j] or "") and texts[j] not in LOCATION_TYPES:
                        candidate = texts[j]
                        if candidate and not candidate.endswith(" logo") and not candidate.startswith("http"):
                            title = candidate

            # Walk forwards: location_type may follow the date
            k = i + 1
            if k < len(texts) and texts[k] in LOCATION_TYPES:
                location_type = texts[k]

            # If location not found backwards, check "City · LocationType" pattern
            if not location and k < len(texts):
                nxt = texts[k] if k < len(texts) else ""
                if nxt and " · " in nxt:
                    parts = nxt.split(" · ")
                    location = parts[0].strip()
                    if not location_type and len(parts) > 1:
                        location_type = parts[1].strip()

            start, end, is_current = _parse_date_range(date_str)

            positions.append(Experience(
                title=title,
                employment_type=employment_type,
                company=CompanyRef(name=company_name) if company_name else None,
                location=location,
                location_type=location_type,
                start=DatePart(**start) if start else None,
                end=DatePart(**end) if end else None,
                is_current=is_current,
                duration_months=_parse_duration_months(date_str),
                description=None,
                skills=[],
            ))
        i += 1
    return positions


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
    """Parse 'Jul 2025' into {'year': 2025, 'month': 7, 'day': None, 'iso': '2025-07'}."""
    m = re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$", s.strip())
    if not m:
        return None
    month = months.get(m.group(1))
    year = int(m.group(2))
    if not month:
        return None
    return {"year": year, "month": month, "day": None, "iso": f"{year:04d}-{month:02d}"}


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


def map_education_from_rsc(texts: list[str]) -> list[Education]:
    """Map education from the main page texts.

    Education appears as school name in the "Company · School" pattern in the topcard,
    or as a standalone school name. For the current transport, education is limited
    to the school name in the topcard summary.
    """
    schools: list[Education] = []
    for t in texts:
        if " · " in t:
            parts = t.split(" · ")
            if len(parts) == 2:
                school_name = parts[1].strip()
                if school_name and len(school_name) > 3:
                    schools.append(Education(school=school_name))
                    break
    return schools


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
    """Extract profile photo and background photo URLs from the main page texts."""
    profile_imgs: list[Image] = []
    bg_imgs: list[Image] = []

    in_profile_photo = False
    for t in texts:
        if t == "Profile photo":
            in_profile_photo = True
            continue
        if t == "Cover photo":
            in_profile_photo = False
            continue
        if t.startswith("https://media.licdn.com/dms/image") and "profile-displayphoto" in t:
            if in_profile_photo or not bg_imgs:
                profile_imgs.append(Image(url=t, expires_at=parse_image_expiry(t)))
        elif t.startswith("https://media.licdn.com/dms/image") and "profile-displaybackgroundimage" in t:
            bg_imgs.append(Image(url=t, expires_at=parse_image_expiry(t)))

    return ProfileImages(profile=profile_imgs, background=bg_imgs)
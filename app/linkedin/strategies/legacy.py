"""Voyager REST sub-resource strategy (universal approach).

Hits the individual Voyager REST sub-resource endpoints that are still alive:
  /voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={slug}
  /voyager/api/identity/profiles/{slug}/positionGroups
  /voyager/api/identity/profiles/{slug}/skills
  /voyager/api/identity/profiles/{slug}/educations
  /voyager/api/identity/profiles/{slug}/languages
  /voyager/api/identity/profiles/{slug}/certifications

These return the normalized JSON envelope (data + included) that our URN graph
resolver was built for. This is the universal approach — it works for ANY profile
without layout-specific text parsing. The profileView endpoint is 410 Gone, but
the sub-resources still work.

This is the approach used by Skrapp, PhantomBuster, and every other LinkedIn
scraping tool: hit the Voyager REST endpoints with the auth cookies and parse
the normalized JSON response.
"""

from __future__ import annotations

from typing import Any, ClassVar

import orjson
import structlog

from app.linkedin.client import ClassifiedResponse, Outcome
from app.linkedin.strategies import FetchResult

log = structlog.get_logger("strategy.voyager_rest")

VOYAGER_BASE = "https://www.linkedin.com/voyager/api"

# Sub-resources that return normalized JSON. Some are deprecated (410) but we
# try them all — the ones that work contribute their data to the merged envelope.
# Sub-resources still worth requesting.
#
# Deliberately does NOT include `skills`, `educations` (both 410 Gone) or `volunteer`
# (404). Those were verified dead against live traffic — see docs/REVERSE_ENGINEERING.md.
# Requesting them cost three round-trips per profile and returned tombstones every
# time, which matters twice over: latency on a serverless host with a request
# deadline, and wasted requests against a session LinkedIn rate-limits aggressively.
#
# The ones kept below answer 200. They are frequently empty (not every profile has
# honors or courses), which is a different thing from being retired.
SUB_RESOURCES = [
    "certifications",
    "languages",
    "projects",
    "honors",
    "courses",
]

# Sub-resources confirmed retired by LinkedIn, kept as documentation so nobody
# re-adds them after seeing an empty section and assuming an oversight.
RETIRED_SUB_RESOURCES = {
    "skills": 410,
    "educations": 410,
    "profileView": 410,
    "volunteer": 404,
}


class LegacyStrategy:
    """Voyager REST sub-resource strategy. Universal — works for any profile.

    Fetches the dash profile (for core profile data + URN) and all available
    sub-resources, then merges them into a single normalized envelope.
    """

    name: ClassVar[str] = "voyager_rest"
    requires_auth: ClassVar[bool] = True
    provides: ClassVar[set[str]] = {
        "profile", "experience", "education", "skills",
        "certifications", "languages", "projects", "honors",
        "volunteer", "courses",
    }

    async def fetch(self, slug: str, client: Any) -> FetchResult | None:
        # 1. Fetch the dash profile for core data + URN.
        #
        # A failure here is NOT fatal. These sub-resources have independent
        # availability — dash/profiles rate-limits far more aggressively than
        # positionGroups, and several legacy sub-resources are 410 Gone permanently.
        # Aborting the whole strategy on the first failure threw away the experience
        # data that had already been fetched successfully.
        dash_url = f"{VOYAGER_BASE}/identity/dash/profiles?q=memberIdentity&memberIdentity={slug}"
        resp = await client.fetch(dash_url)
        dash_data: dict | None = None
        if resp.outcome is Outcome.OK:
            dash_data = _decode(resp)
        else:
            log.warning("voyager_rest.dash_failed", slug=slug, outcome=resp.outcome.value)

        profile_urn = _extract_urn(dash_data) if dash_data else None

        # 2. Fetch positionGroups (experience) — this endpoint is still alive
        pos_url = f"{VOYAGER_BASE}/identity/profiles/{slug}/positionGroups"
        resp = await client.fetch(pos_url)
        pos_data = _decode(resp) if resp.outcome is Outcome.OK else None

        # 3. Fetch all other sub-resources
        sub_results: dict[str, dict | None] = {}
        for resource in SUB_RESOURCES:
            url = f"{VOYAGER_BASE}/identity/profiles/{slug}/{resource}"
            resp = await client.fetch(url)
            if resp.outcome is Outcome.OK:
                sub_results[resource] = _decode(resp)
            else:
                sub_results[resource] = None

        # The profile URN also lives inside every position's entityUrn, as
        # urn:li:fs_position:(<profileId>,<positionId>). That is the fallback when
        # dash/profiles was unavailable.
        if not profile_urn and pos_data:
            profile_urn = _urn_from_positions(pos_data)

        # 4. Merge whatever came back into a single normalized envelope.
        merged = _merge_envelopes(dash_data or {}, pos_data, sub_results)

        # Succeed only if something actually carried data. An envelope of nothing is
        # a failure, and reporting it as success would stop the fallback chain.
        if not merged.get("included") and not merged.get("data"):
            log.info("voyager_rest.no_data", slug=slug)
            return None

        return FetchResult(payload=merged, profile_urn=profile_urn, source=self.name)


def _decode(resp: ClassifiedResponse) -> dict | None:
    try:
        data = orjson.loads(resp.body_bytes)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_urn(payload: dict) -> str | None:
    """Extract the profile URN from the dash profile response."""
    # dash/profiles returns *elements in data, each pointing to a profile entity
    data = payload.get("data", {})
    if isinstance(data, dict):
        elements = data.get("*elements") or data.get("elements")
        if isinstance(elements, list) and elements:
            urn = elements[0]
            if isinstance(urn, str) and urn.startswith("urn:"):
                return urn
    # Fallback: scan included for a Profile entity
    for ent in payload.get("included", []):
        if not isinstance(ent, dict):
            continue
        t = ent.get("$type", "")
        if "Profile" in t and "Dash" not in t:
            urn = ent.get("entityUrn") or ent.get("objectUrn")
            if isinstance(urn, str) and urn.startswith("urn:"):
                return urn
    # Also check for dash profile
    for ent in payload.get("included", []):
        if not isinstance(ent, dict):
            continue
        urn = ent.get("entityUrn") or ent.get("objectUrn")
        if isinstance(urn, str) and urn.startswith("urn:li:fsd_profile:"):
            return urn
    return None


def _urn_from_positions(positions: dict) -> str | None:
    """Recover the profile URN from a position entityUrn.

    Positions are keyed as urn:li:fs_position:(<profileId>,<positionId>), so the
    profile id can be read straight out of the compound key when the dedicated
    profile endpoint is unavailable.
    """
    for ent in positions.get("included", []):
        if not isinstance(ent, dict):
            continue
        urn = ent.get("entityUrn")
        if isinstance(urn, str) and urn.startswith("urn:li:fs_position:("):
            inner = urn.split("(", 1)[1].rstrip(")")
            profile_id = inner.split(",", 1)[0].strip()
            if profile_id:
                return f"urn:li:fsd_profile:{profile_id}"
    return None


# Keys that describe a Rest.li collection response rather than profile content.
# They must not reach the merged data root; see _merge_envelopes.
_COLLECTION_META = {"entityUrn", "paging", "*elements", "elements", "$type"}


def _merge_envelopes(
    dash: dict,
    positions: dict | None,
    sub_results: dict[str, dict | None],
) -> dict:
    """Merge multiple Voyager normalized JSON responses into one envelope.

    Each response has the shape {"data": {...}, "included": [...]}.
    We concatenate all `included` arrays and merge the `data` dicts.
    """
    included: list = []
    data: dict = {}

    # Dash profile (core profile data).
    #
    # Only the entities are merged, never the collection-response wrapper. A dash
    # query answers with {entityUrn, paging, *elements} describing the QUERY, and
    # `*elements` there points at the profile itself. Merging that to the data root
    # collides with the experience mapper's `elements` key, which then maps the
    # Profile entity as if it were a position — one empty job with the profile's
    # country as its location. The Position entities live in `included` and are
    # reached through the URN graph.
    if isinstance(dash.get("included"), list):
        included.extend(dash["included"])
    if isinstance(dash.get("data"), dict):
        data.update(
            {k: v for k, v in dash["data"].items() if k not in _COLLECTION_META}
        )

    # Position groups (experience)
    if positions:
        if isinstance(positions.get("included"), list):
            included.extend(positions["included"])
        if isinstance(positions.get("data"), dict):
            data.setdefault("positionGroups", positions["data"])

    # Sub-resources
    for name, result in sub_results.items():
        if result:
            if isinstance(result.get("included"), list):
                included.extend(result["included"])
            if isinstance(result.get("data"), dict):
                data.setdefault(name, result["data"])

    return {"data": data, "included": included}
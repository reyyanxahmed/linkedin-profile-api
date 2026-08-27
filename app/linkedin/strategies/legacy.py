"""Legacy REST strategy (Generation 1).

Hits /voyager/api/identity/profiles/{slug}/profileView and its sub-resources. Most
convenient single call; partially deprecated but still the most reliable fallback.
Does not need a queryId or decorationId — works whenever a session is available.
"""

from __future__ import annotations

from typing import Any, ClassVar

import orjson

from app.linkedin import endpoints
from app.linkedin.client import ClassifiedResponse, Outcome
from app.linkedin.strategies import FetchResult


class LegacyStrategy:
    """Legacy REST profileView strategy. Requires auth. No queryId needed."""

    name: ClassVar[str] = "voyager_legacy"
    requires_auth: ClassVar[bool] = True
    provides: ClassVar[set[str]] = {
        "profile", "experience", "education", "skills",
        "certifications", "languages", "projects", "honors",
    }

    async def fetch(self, slug: str, client: Any) -> FetchResult | None:
        url = endpoints.legacy_profile_view(slug)
        resp = await client.fetch(url)
        if not _is_ok(resp):
            return None
        payload = _decode(resp)
        if payload is None:
            return None
        # profileView returns entityUrn on the profile entity; pull it for the URN.
        profile_urn = _extract_profile_urn(payload)
        return FetchResult(payload=payload, profile_urn=profile_urn, source=self.name)


def _is_ok(resp: ClassifiedResponse) -> bool:
    return resp.outcome is Outcome.OK and bool(resp.body_bytes)


def _decode(resp: ClassifiedResponse) -> dict | None:
    try:
        data = orjson.loads(resp.body_bytes)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_profile_urn(payload: dict) -> str | None:
    # Legacy profileView nests the profile under data; the URN is entityUrn of the
    # profile entity. Baseline shape; calibrate against a fixture when available.
    data = payload.get("data", {})
    if isinstance(data, dict):
        urn = data.get("entityUrn") or data.get("profileUrn") or data.get("publicIdentifier")
        if isinstance(urn, str) and urn.startswith("urn:"):
            return urn
    # Fallback: scan included for a Profile entity.
    for ent in payload.get("included", []):
        if isinstance(ent, dict) and "Profile" in str(ent.get("$type", "")):
            urn = ent.get("entityUrn")
            if isinstance(urn, str):
                return urn
    return None
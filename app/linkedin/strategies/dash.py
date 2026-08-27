"""Dash strategy (Generation 2).

Hits /voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={slug}&decorationId={id}.
Needs a decorationId from queries.yaml. Also useful as the slug->URN resolver for GraphQL.
"""

from __future__ import annotations

from typing import Any, ClassVar

import orjson
import structlog

from app.errors import ConfigError
from app.linkedin import endpoints
from app.linkedin.client import ClassifiedResponse, Outcome
from app.linkedin.strategies import FetchResult

PLACEHOLDER = "PLACEHOLDER"

log = structlog.get_logger("strategy.dash")


class DashStrategy:
    """Dash profile strategy. Requires auth + a real decorationId in queries.yaml."""

    name: ClassVar[str] = "voyager_dash"
    requires_auth: ClassVar[bool] = True
    provides: ClassVar[set[str]] = {
        "profile", "experience", "education", "skills",
        "certifications", "languages", "projects", "honors", "courses",
    }

    def __init__(self, decoration_id: str) -> None:
        self.decoration_id = decoration_id

    async def fetch(self, slug: str, client: Any) -> FetchResult | None:
        if not self.decoration_id or self.decoration_id == PLACEHOLDER:
            raise ConfigError(
                "dash.full_profile_decoration_id in app/linkedin/queries.yaml is still "
                "PLACEHOLDER. Obtain a real value with: "
                "python scripts/extract_query_ids.py capture.har  "
                "(look for decorationId in /identity/dash/profiles URLs), then paste it "
                "into queries.yaml."
            )
        url = endpoints.dash_profile(slug, self.decoration_id)
        resp = await client.fetch(url)
        if resp.outcome is Outcome.NOT_FOUND:
            return None
        if resp.outcome is not Outcome.OK:
            return None
        payload = _decode(resp)
        if payload is None:
            return None
        # The dash memberIdentity query returns the profile URN in data.*profile or
        # data.elements[0].entityUrn. Baseline shape; calibrate against a fixture.
        urn = _extract_urn(payload)
        return FetchResult(payload=payload, profile_urn=urn, source=self.name)


def _decode(resp: ClassifiedResponse) -> dict | None:
    try:
        data = orjson.loads(resp.body_bytes)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_urn(payload: dict) -> str | None:
    data = payload.get("data", {})
    if isinstance(data, dict):
        # Star-key *profile points to the profile entity URN.
        urn = data.get("*profile") or data.get("profileUrn")
        if isinstance(urn, str) and urn.startswith("urn:"):
            return urn
        # elements[0].entityUrn is the dash form.
        elements = data.get("elements") or data.get("*elements")
        if isinstance(elements, list) and elements:
            first = elements[0]
            if isinstance(first, str) and first.startswith("urn:"):
                return first
            if isinstance(first, dict):
                urn = first.get("entityUrn") or first.get("profileUrn")
                if isinstance(urn, str):
                    return urn
    return None
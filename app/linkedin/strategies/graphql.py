"""GraphQL strategy (Generation 3).

Hits /voyager/api/graphql?queryId={id}&variables=(profileUrn:urn%3Ali%3Afsd_profile%3A...).
Needs a queryId from queries.yaml AND a profileUrn (resolved from the slug via dash or
legacy first). This is the highest-fidelity strategy and also the most fragile: the
queryId rotates with LinkedIn frontend deploys.
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

log = structlog.get_logger("strategy.graphql")


class GraphQLStrategy:
    """GraphQL cards strategy. Requires auth + queryId + a pre-resolved profile URN."""

    name: ClassVar[str] = "voyager_graphql"
    requires_auth: ClassVar[bool] = True
    provides: ClassVar[set[str]] = {
        "profile", "experience", "education", "skills",
        "certifications", "languages", "projects", "honors", "volunteer", "courses",
    }

    def __init__(self, profile_cards_query_id: str) -> None:
        self.query_id = profile_cards_query_id

    async def fetch(self, slug: str, client: Any, *, profile_urn: str | None = None) -> FetchResult | None:
        if not self.query_id or self.query_id == PLACEHOLDER:
            raise ConfigError(
                "graphql.profile_cards_query_id in app/linkedin/queries.yaml is still "
                "PLACEHOLDER. Obtain a real value with: "
                "python scripts/extract_query_ids.py capture.har  "
                "(look for queryId in /voyager/api/graphql URLs), then paste it into "
                "queries.yaml."
            )
        if not profile_urn:
            # GraphQL needs a profileUrn, not a slug. The orchestrator resolves the URN
            # via dash or legacy first and passes it in. If we have no URN, we cannot run.
            return None
        url = endpoints.graphql_profile_cards(self.query_id, profile_urn)
        resp = await client.fetch(url)
        if resp.outcome is Outcome.NOT_FOUND:
            return None
        if resp.outcome is not Outcome.OK:
            return None
        payload = _decode(resp)
        if payload is None:
            return None
        return FetchResult(payload=payload, profile_urn=profile_urn, source=self.name)


def _decode(resp: ClassifiedResponse) -> dict | None:
    try:
        data = orjson.loads(resp.body_bytes)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
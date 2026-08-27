"""Strategy protocol and shared types.

Single responsibility: define the Strategy interface that orchestrator.py depends on,
plus the FetchResult envelope strategies return. No business logic here.

A strategy is a fetch adapter: given a slug + an authenticated (or unauthenticated)
client, fetch the profile from one endpoint generation and return a raw payload that
section mappers can consume. Strategies do not normalize — they fetch and decode.

Each strategy declares:
  - name: stable string recorded in meta.source
  - requires_auth: whether it needs a live session (public_html does not)
  - provides: set of section names it can populate, for supplement merging
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class FetchResult:
    """The raw payload returned by a strategy, plus metadata for the orchestrator.

    `payload` is the decoded JSON in LinkedIn's normalized-envelope shape (or the
    public-HTML JSON-LD dict for the public strategy). Section mappers read from it.
    `profile_urn` is set when the strategy also resolved the slug to a URN.
    """

    payload: dict | None
    profile_urn: str | None = None
    source: str = ""
    # Sections this payload actually populated (filled by the orchestrator after mapping).
    populated_sections: set[str] = field(default_factory=set)


@runtime_checkable
class Strategy(Protocol):
    """A fetch strategy. See app/linkedin/orchestrator.py for the chain order."""

    name: str
    requires_auth: bool
    provides: set[str]

    async def fetch(self, slug: str, client: Any) -> FetchResult | None:
        """Fetch the profile. Return None if the strategy cannot run (e.g. missing
        queryId) or the profile was not found via this endpoint. Raise only for
        unexpected errors — the orchestrator catches and skips.
        """
        ...
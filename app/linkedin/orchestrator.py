"""Fetch orchestrator: strategy chain, URN resolution, and section merging.

Single responsibility: run the strategies in order, resolve the slug to a profile URN
for GraphQL, merge section payloads additively, and produce the section-level payloads
that mappers consume. The orchestrator never 500s on a strategy failure — it logs and
continues down the chain.

See BUILD_SPEC.md section 6.8 for the algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
import yaml

from app.config import Settings
from app.errors import ConfigError
from app.linkedin.client import LinkedInClient
from app.linkedin.strategies import FetchResult, Strategy
from app.linkedin.strategies.dash import DashStrategy
from app.linkedin.strategies.flagship_web import FlagshipWebStrategy
from app.linkedin.strategies.graphql import GraphQLStrategy
from app.linkedin.strategies.legacy import LegacyStrategy
from app.linkedin.strategies.public_html import PublicHtmlStrategy

log = structlog.get_logger("orchestrator")


STRATEGY_ORDER: list[type[Strategy]] = [
    # GraphQLStrategy,  # highest fidelity, needs queryId + URN; inserted at runtime
    # DashStrategy,     # needs decorationId; resolves URN for GraphQL
    # LegacyStrategy,   # no queryId, most reliable fallback
    # PublicHtmlStrategy,  # unauthenticated, always available
]


@dataclass
class OrchestratorResult:
    """The merged raw payloads, one per section, plus provenance for meta."""

    sections: dict[str, Any] = field(default_factory=dict)
    profile_urn: str | None = None
    source: str = ""
    supplemented_by: list[str] = field(default_factory=list)


def load_queries(path: str = "app/linkedin/queries.yaml") -> dict:
    """Load queries.yaml. Returns a dict; missing keys default to empty strings."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data
    except FileNotFoundError:
        log.warning("queries.yaml not found", path=path)
        return {}


def build_strategies(queries: dict) -> list[Strategy]:
    """Construct the strategy chain from queries.yaml values.

    The flagship-web RSC strategy is the primary path (LinkedIn migrated to this
    transport). Voyager strategies (GraphQL, dash, legacy) are kept as fallbacks
    in case RSC is unavailable or returns partial data.

    Strategies with placeholder config still get constructed; they raise ConfigError
    at fetch time (caught by the orchestrator) and degrade to the next strategy.
    """
    graphql_cfg = queries.get("graphql", {}) or {}
    dash_cfg = queries.get("dash", {}) or {}
    return [
        FlagshipWebStrategy(),  # primary — LinkedIn's current transport
        GraphQLStrategy(graphql_cfg.get("profile_cards_query_id", "")),
        DashStrategy(dash_cfg.get("full_profile_decoration_id", "")),
        LegacyStrategy(),
        PublicHtmlStrategy(),
    ]


class Orchestrator:
    """Runs the strategy chain and merges results.

    Algorithm (BUILD_SPEC.md 6.8):
      1. Try strategies in order. First parseable payload becomes primary.
      2. If core sections complete, stop.
      3. If sections missing, call lower-priority strategies that provide them;
         merge into empty sections only. Record supplements.
      4. ConfigError -> log + skip + continue.
      5. If every strategy fails, signal stale-cache fallback to the caller.
    """

    def __init__(self, settings: Settings, queries_path: str = "app/linkedin/queries.yaml") -> None:
        self.settings = settings
        self.queries = load_queries(queries_path)
        self.strategies = build_strategies(self.queries)

    async def fetch(self, slug: str, client: LinkedInClient) -> OrchestratorResult:
        result = OrchestratorResult()
        primary: FetchResult | None = None
        primary_strategy: Strategy | None = None

        # Phase 1: find the primary. GraphQL needs a URN, so resolve it first via
        # dash or legacy. We attempt GraphQL last (after URN resolution) but record
        # it as highest priority in the chain.
        profile_urn: str | None = None
        # First, try dash/legacy purely to resolve the URN (cheap; one call each).
        for strat in self.strategies:
            if not strat.requires_auth and strat.name == "public_html":
                continue
            if isinstance(strat, GraphQLStrategy):
                continue  # GraphQL handled after URN resolution
            try:
                if isinstance(strat, DashStrategy):
                    r = await strat.fetch(slug, client)
                elif isinstance(strat, LegacyStrategy):
                    r = await strat.fetch(slug, client)
                else:
                    r = await strat.fetch(slug, client)
            except ConfigError as e:
                log.warning("strategy.config_error", strategy=strat.name, error=str(e))
                continue
            except Exception as e:
                log.warning("strategy.error", strategy=strat.name, error=str(e))
                continue
            if r and r.payload is not None:
                if not primary:
                    primary = r
                    primary_strategy = strat
                if r.profile_urn and not profile_urn:
                    profile_urn = r.profile_urn

        # Now attempt GraphQL with the resolved URN (if we have one).
        if profile_urn:
            for strat in self.strategies:
                if not isinstance(strat, GraphQLStrategy):
                    continue
                try:
                    r = await strat.fetch(slug, client, profile_urn=profile_urn)
                except ConfigError as e:
                    log.warning("strategy.config_error", strategy=strat.name, error=str(e))
                    continue
                except Exception as e:
                    log.warning("strategy.error", strategy=strat.name, error=str(e))
                    continue
                if r and r.payload is not None:
                    # GraphQL is highest priority; it becomes the primary.
                    primary = r
                    primary_strategy = strat
                    break

        # If no authenticated strategy produced a payload, try the public fallback.
        if not primary:
            for strat in self.strategies:
                if strat.requires_auth:
                    continue
                try:
                    r = await strat.fetch(slug, client)
                except Exception as e:
                    log.warning("strategy.error", strategy=strat.name, error=str(e))
                    continue
                if r and r.payload is not None:
                    primary = r
                    primary_strategy = strat
                    break

        if not primary or not primary_strategy:
            log.info("orchestrator.no_strategy_succeeded", slug=slug)
            return result

        result.sections = {"_primary": primary.payload, "_source": primary.source}
        result.profile_urn = primary.profile_urn or profile_urn
        result.source = primary.source
        return result
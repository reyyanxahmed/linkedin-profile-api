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


def build_strategies(queries: dict, offline_mode: bool = False, fixture_dir: str = "") -> list[Strategy]:
    """Construct the strategy chain from queries.yaml values.

    The flagship-web RSC strategy is the primary path (LinkedIn migrated to this
    transport). Voyager strategies (GraphQL, dash, legacy) are kept as fallbacks
    in case RSC is unavailable or returns partial data.

    In offline_mode with fixture_dir set, the FlagshipWebStrategy serves from saved
    RSC fixtures on disk instead of hitting LinkedIn — no network needed.

    Strategies with placeholder config still get constructed; they raise ConfigError
    at fetch time (caught by the orchestrator) and degrade to the next strategy.
    """
    graphql_cfg = queries.get("graphql", {}) or {}
    dash_cfg = queries.get("dash", {}) or {}
    return [
        FlagshipWebStrategy(offline_mode=offline_mode, fixture_dir=fixture_dir),
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
        self.strategies = build_strategies(
            self.queries,
            offline_mode=settings.offline_mode,
            fixture_dir=settings.fixture_dir,
        )

    async def fetch(self, slug: str, client: LinkedInClient) -> OrchestratorResult:
        result = OrchestratorResult()
        primary: FetchResult | None = None
        primary_strategy: Strategy | None = None
        supplemented: list[str] = []

        # Phase 1: try the flagship-web RSC strategy for the profile header
        # (name, headline, location, photos, followers) and experience.
        flagship_result: FetchResult | None = None
        for strat in self.strategies:
            if isinstance(strat, FlagshipWebStrategy):
                try:
                    r = await strat.fetch(slug, client)
                except Exception as e:
                    log.warning("strategy.error", strategy=strat.name, error=str(e))
                if r and r.payload is not None:
                    flagship_result = r
                break

        # Phase 2: try the Voyager REST strategy for structured data
        # (experience positions, skills, education, languages, certifications).
        # This is the universal approach — works for any profile.
        voyager_result: FetchResult | None = None
        for strat in self.strategies:
            if isinstance(strat, LegacyStrategy):
                try:
                    r = await strat.fetch(slug, client)
                except Exception as e:
                    log.warning("strategy.error", strategy=strat.name, error=str(e))
                if r and r.payload is not None:
                    voyager_result = r
                break

        # Phase 3: merge — prefer Voyager for structured data, flagship for header.
        if voyager_result and flagship_result:
            # Both worked: use Voyager as primary (it has the full normalized
            # envelope), and supplement the profile header from flagship.
            primary = voyager_result
            primary_strategy = next(s for s in self.strategies if isinstance(s, LegacyStrategy))
            supplemented.append("flagship_web_rsc")
            # Merge flagship's main_texts into the payload for header fields
            # that Voyager may not have (photos, followers, about).
            if isinstance(primary.payload, dict):
                primary.payload["_flagship_main_texts"] = flagship_result.payload.get("main_texts", [])
                primary.payload["_flagship_about_texts"] = flagship_result.payload.get("about_texts", [])
        elif voyager_result:
            primary = voyager_result
            primary_strategy = next(s for s in self.strategies if isinstance(s, LegacyStrategy))
        elif flagship_result:
            primary = flagship_result
            primary_strategy = next(s for s in self.strategies if isinstance(s, FlagshipWebStrategy))
        else:
            # Phase 4: try GraphQL and public HTML as last resorts.
            for strat in self.strategies:
                if isinstance(strat, (FlagshipWebStrategy, LegacyStrategy)):
                    continue
                if not strat.requires_auth:
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
        result.profile_urn = primary.profile_urn
        result.source = primary.source
        return result
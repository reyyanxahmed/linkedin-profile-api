"""URL builders for the three Voyager endpoint generations.

Single responsibility: construct the exact request URLs for each generation, including
the Rest.li-encoded GraphQL variables parameter. No I/O, pure functions. Unit-tested
against the encoding gotchas (URN colons percent-encoded, parentheses literal).

See BUILD_SPEC.md section 4.2 for the endpoint shapes.
"""

from __future__ import annotations

import urllib.parse as up

VOYAGER_BASE = "https://www.linkedin.com/voyager/api"

# --- Legacy (Generation 1) ---------------------------------------------------

def legacy_profile_view(slug: str) -> str:
    """GET /voyager/api/identity/profiles/{slug}/profileView"""
    return f"{VOYAGER_BASE}/identity/profiles/{up.quote(slug)}/profileView"


def legacy_subresource(slug: str, resource: str) -> str:
    """Sub-resources: positionGroups, educations, skills, certifications, languages, projects, honors."""
    return f"{VOYAGER_BASE}/identity/profiles/{up.quote(slug)}/{resource}"


# --- Dash (Generation 2) -----------------------------------------------------

def dash_profile(slug: str, decoration_id: str) -> str:
    """GET /voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={slug}&decorationId={decoration}"""
    params = {
        "q": "memberIdentity",
        "memberIdentity": slug,
        "decorationId": decoration_id,
    }
    qs = up.urlencode(params)
    return f"{VOYAGER_BASE}/identity/dash/profiles?{qs}"


# --- GraphQL (Generation 3) --------------------------------------------------

def graphql_profile_cards(query_id: str, profile_urn: str) -> str:
    """GET /voyager/api/graphql?queryId={id}&variables=(profileUrn:urn%3Ali%3Afsd_profile%3A{id})

    The variables parameter uses LinkedIn's Rest.li encoding, NOT JSON:
      - parentheses are literal
      - the URN's own colons are percent-encoded (%3A)
    This is the #1 encoding gotcha. Build carefully and unit-test.
    """
    # Percent-encode the URN's colons but leave the rest of the structure literal.
    urn_encoded = profile_urn.replace(":", "%3A")
    variables = f"(profileUrn:{urn_encoded})"
    # urlencode would mangle the parentheses, so build the query string by hand.
    return f"{VOYAGER_BASE}/graphql?queryId={up.quote(query_id)}&variables={variables}"


# --- Public HTML (unauthenticated fallback) ----------------------------------

def public_profile_html(slug: str) -> str:
    """GET https://www.linkedin.com/in/{slug} — returns HTML with JSON-LD blocks."""
    return f"https://www.linkedin.com/in/{up.quote(slug)}"
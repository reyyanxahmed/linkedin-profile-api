"""Tests for app.linkedin.endpoints — URL builders and Rest.li encoding.

The critical test: graphql_profile_cards must produce the exact Rest.li-encoded
variables parameter with literal parentheses and percent-encoded URN colons.
"""

from __future__ import annotations

import urllib.parse as up

from app.linkedin.endpoints import (
    dash_profile,
    graphql_profile_cards,
    legacy_profile_view,
    legacy_subresource,
    public_profile_html,
)


class TestLegacy:
    def test_profile_view_url(self) -> None:
        url = legacy_profile_view("some-slug")
        assert url == "https://www.linkedin.com/voyager/api/identity/profiles/some-slug/profileView"

    def test_profile_view_url_encodes_slug(self) -> None:
        url = legacy_profile_view("中文")
        assert up.quote("中文") in url

    def test_subresource_url(self) -> None:
        url = legacy_subresource("some-slug", "skills")
        assert url.endswith("/identity/profiles/some-slug/skills")


class TestDash:
    def test_dash_url_has_params(self) -> None:
        url = dash_profile("some-slug", "deco-123")
        assert "q=memberIdentity" in url
        assert "memberIdentity=some-slug" in url
        assert "decorationId=deco-123" in url
        assert url.startswith("https://www.linkedin.com/voyager/api/identity/dash/profiles?")


class TestGraphQLEncoding:
    def test_parentheses_are_literal(self) -> None:
        url = graphql_profile_cards("query-hash-123", "urn:li:fsd_profile:ACoAA123")
        # The variables parameter must contain literal parentheses (not %28).
        assert "variables=(profileUrn:" in url
        assert url.endswith(")")

    def test_urn_colons_are_percent_encoded(self) -> None:
        urn = "urn:li:fsd_profile:ACoAA123"
        url = graphql_profile_cards("q1", urn)
        # The URN's colons become %3A, but the surrounding structure is literal.
        encoded_urn = urn.replace(":", "%3A")
        assert f"variables=(profileUrn:{encoded_urn})" in url

    def test_query_id_is_url_encoded(self) -> None:
        url = graphql_profile_cards("query with space", "urn:li:fsd_profile:X")
        # queryId should be percent-encoded.
        assert "queryId=query%20with%20space" in url


class TestPublicHtml:
    def test_public_profile_url(self) -> None:
        assert public_profile_html("some-slug") == "https://www.linkedin.com/in/some-slug"

    def test_public_profile_url_encodes_slug(self) -> None:
        url = public_profile_html("中文")
        assert up.quote("中文") in url
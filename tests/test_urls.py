"""Tests for app.urls.normalize_profile_url.

Covers every accept/reject case listed in BUILD_SPEC.md section 6.1.
"""

from __future__ import annotations

import pytest

from app.errors import InvalidUrlError
from app.urls import normalize_profile_url


class TestAccept:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://www.linkedin.com/in/some-slug", "some-slug"),
            ("https://linkedin.com/in/some-slug/", "some-slug"),
            ("http://in.linkedin.com/in/some-slug", "some-slug"),
            ("linkedin.com/in/some-slug?originalSubdomain=in", "some-slug"),
            ("www.linkedin.com/in/some-slug/?trk=public_profile", "some-slug"),
            ("some-slug", "some-slug"),
            # percent-encoded unicode slug
            ("https://www.linkedin.com/in/%E4%B8%AD%E6%96%87", "中文"),
            # trailing fragment
            ("https://www.linkedin.com/in/some-slug#experience", "some-slug"),
            # uppercase host, lowercase slug preserved
            ("https://WWW.LINKEDIN.COM/IN/Some-Slug", "Some-Slug"),
            # /pub/ legacy form (non-dir)
            ("https://www.linkedin.com/pub/some-slug", "some-slug"),
            # locale-prefixed /in/
            ("https://www.linkedin.com/en/in/some-slug", "some-slug"),
            # bare slug with hyphens
            ("reyyanxahmed", "reyyanxahmed"),
            ("first-last-123", "first-last-123"),
        ],
    )
    def test_accept(self, raw: str, expected: str) -> None:
        assert normalize_profile_url(raw) == expected


class TestReject:
    @pytest.mark.parametrize(
        "raw",
        [
            "",  # empty
            "   ",  # whitespace only
            "/company/some-co",  # company path
            "https://www.linkedin.com/company/some-co",
            "/school/some-school",
            "https://www.linkedin.com/school/some-school",
            "/pub/dir/First/Last",
            "https://www.linkedin.com/pub/dir/First/Last",
            "https://www.linkedin.com/feed/",
            "https://example.com/in/some-slug",  # non-linkedin host
            "https://www.linkedin.com/in/",  # empty slug
            "https://www.linkedin.com/",  # no /in/ marker
            "https://www.linkedin.com/jobs/view/123",
        ],
    )
    def test_reject(self, raw: str) -> None:
        with pytest.raises(InvalidUrlError):
            normalize_profile_url(raw)
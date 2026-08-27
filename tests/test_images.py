"""Tests for app.normalize.images — CDN URL expiry parsing."""

from __future__ import annotations

from app.normalize.images import parse_image_expiry


class TestParseExpiry:
    def test_expires_at_param_seconds(self) -> None:
        url = "https://media.licdn.com/dms/image/abc?expires_at=1735689600"
        assert parse_image_expiry(url) == "2025-01-01T00:00:00Z"

    def test_e_param_compact(self) -> None:
        url = "https://media.licdn.com/dms/image/abc?e=1735689600"
        assert parse_image_expiry(url) == "2025-01-01T00:00:00Z"

    def test_no_expiry_returns_none(self) -> None:
        url = "https://media.licdn.com/dms/image/abc?other=1"
        assert parse_image_expiry(url) is None

    def test_none_url(self) -> None:
        assert parse_image_expiry(None) is None

    def test_invalid_value_returns_none(self) -> None:
        url = "https://media.licdn.com/dms/image/abc?expires_at=notanumber"
        assert parse_image_expiry(url) is None
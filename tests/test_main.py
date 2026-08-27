"""Tests for app.main — the FastAPI API surface.

Gate 4: boots with empty LI_SESSIONS, /health returns honest state, /docs renders,
a request with no API key returns 401, and the request-id middleware sets the header.

Uses httpx.AsyncClient(app=...) which runs the app's lifespan automatically. No
network (the app has no live sessions, so every authenticated strategy skips).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Build the app with test settings patched in. Empty LI_SESSIONS -> Gate 4 scenario."""
    import os
    os.environ["LI_SESSIONS"] = ""
    os.environ["API_KEY"] = "test-key-12345"
    os.environ["REDIS_URL"] = ""  # no redis in tests
    # Patch the settings object in app.main and app.config so the app sees test values.
    import app.config
    import app.main
    # Build fresh settings from the env we just set.
    new_settings = app.config.Settings()
    monkeypatch.setattr(app.main, "settings", new_settings)
    monkeypatch.setattr(app.config, "settings", new_settings)
    return app.main.app


@pytest.fixture
async def client(app):  # type: ignore[no-untyped-def]
    """Run the app with its lifespan, then expose an httpx client against it."""
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


class TestHealth:
    async def test_health_returns_ok_with_empty_sessions(self, client) -> None:
        r = await client.get("/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        # Honest state: empty pool, redis absent.
        assert body["sessions"]["total"] == 0
        assert body["sessions"]["available"] == 0
        assert body["sessions"]["cooling"] == 0
        assert body["redis"] is False

    async def test_health_has_no_token_material(self, client) -> None:
        r = await client.get("/v1/health")
        body = str(r.json())
        assert "li_at" not in body
        assert "ajax:" not in body

    async def test_health_is_unauthenticated(self, client) -> None:
        # No API key header, still 200.
        r = await client.get("/v1/health")
        assert r.status_code == 200


class TestDocs:
    async def test_docs_renders(self, client) -> None:
        r = await client.get("/docs")
        assert r.status_code == 200
        # FastAPI's swagger HTML contains "swagger" somewhere.
        assert "swagger" in r.text.lower() or "openapi" in r.text.lower()

    async def test_openapi_schema(self, client) -> None:
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert schema["info"]["title"] == "LinkedIn Profile API"
        assert "/v1/profile" in schema["paths"]


class TestAuth:
    async def test_profile_without_api_key_returns_401(self, client) -> None:
        r = await client.get("/v1/profile?url=https://www.linkedin.com/in/some-slug")
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == "UNAUTHORIZED"

    async def test_profile_with_wrong_api_key_returns_401(self, client) -> None:
        r = await client.get(
            "/v1/profile?url=https://www.linkedin.com/in/some-slug",
            headers={"X-API-Key": "wrong"},
        )
        assert r.status_code == 401

    async def test_profile_with_correct_api_key_passes_auth(self, client) -> None:
        # With the right key, we get past auth. With no sessions and no cache, the
        # request will fail with PROFILE_NOT_FOUND or ALL_SESSIONS_COOLING — either
        # way, it's NOT a 401.
        r = await client.get(
            "/v1/profile?url=https://www.linkedin.com/in/some-slug",
            headers={"X-API-Key": "test-key-12345"},
        )
        assert r.status_code != 401


class TestRequestId:
    async def test_response_has_request_id_header(self, client) -> None:
        r = await client.get("/v1/health")
        assert "X-Request-ID" in r.headers
        assert len(r.headers["X-Request-ID"]) > 10

    async def test_client_request_id_is_echoed(self, client) -> None:
        r = await client.get("/v1/health", headers={"X-Request-ID": "client-rid-123"})
        assert r.headers["X-Request-ID"] == "client-rid-123"


class TestUrlValidation:
    async def test_invalid_url_returns_400(self, client) -> None:
        r = await client.get(
            "/v1/profile?url=https://example.com/in/slug",
            headers={"X-API-Key": "test-key-12345"},
        )
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "INVALID_URL"

    async def test_empty_url_returns_400(self, client) -> None:
        r = await client.get(
            "/v1/profile?url=",
            headers={"X-API-Key": "test-key-12345"},
        )
        # Empty url -> 422 (FastAPI) or 400 (our handler); both are client errors.
        assert r.status_code in (400, 422)
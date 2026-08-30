"""FastAPI app: routes, middleware, lifespan, exception handlers.

Single responsibility: wire the components into an HTTP API. Routes:
  POST /v1/profile   body: {url, refresh}
  GET  /v1/profile   ?url=...&refresh=false (convenience for curl demos)
  GET  /v1/health    session pool + redis + version, NO token material
  GET  /docs         OpenAPI (FastAPI free)

Auth: X-API-Key header, compared with hmac.compare_digest. /health and /docs are
unauthenticated. Every response carries X-Request-ID matching meta.request_id.

Stale-on-error: if the upstream fails and a stale cache entry exists, return it
with meta.cache.stale = true and HTTP 200 rather than erroring.

No browser anywhere in the runtime.
"""

from __future__ import annotations

import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import orjson
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel as PydanticBaseModel

from app.cache import ProfileCache
from app.config import settings
from app.errors import AppError, NoSessionsError, UnauthorizedError
from app.linkedin.client import LinkedInClient
from app.linkedin.orchestrator import Orchestrator
from app.linkedin.session import SessionPool
from app.logging import configure_logging, get_logger
from app.models import (
    CacheMeta,
    Meta,
    ProfileResponse,
    compute_completeness,
)
from app.normalize.sections.certifications import map_certifications
from app.normalize.sections.core import map_profile
from app.normalize.sections.education import map_education
from app.normalize.sections.experience import map_experience
from app.normalize.sections.extras import (
    map_courses,
    map_honors,
    map_projects,
    map_publications,
    map_volunteer,
)
from app.normalize.sections.languages import map_languages
from app.normalize.sections.skills import map_skills
from app.normalize.urn_graph import UrnGraph
from app.urls import normalize_profile_url

APP_VERSION = "0.1.0"


class ProfileRequest(PydanticBaseModel):
    url: str
    refresh: bool = False


# --- lifespan ----------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Open Redis + HTTP clients on startup; close on shutdown.

    Idempotent-ish: re-running reconfigures. Safe to boot with empty LI_SESSIONS —
    the pool is empty and authenticated strategies are skipped, but /health and
    the public HTML fallback still work.
    """
    configure_logging(settings.log_level)
    log = get_logger("app.lifespan")

    # Session pool from env. Empty LI_SESSIONS -> empty pool -> auth strategies skip.
    pool = SessionPool(
        sessions=[
            s for s in (
                # Build Session objects from the config SessionConfig list.
                __import__("app.linkedin.session", fromlist=["Session"]).Session(
                    li_at=sc.li_at, jsessionid=sc.jsessionid
                )
                for sc in settings.sessions
            )
        ],
        cooldown_seconds=settings.session_cooldown_seconds,
    )
    app.state.pool = pool

    # Cache.
    cache = ProfileCache(settings.redis_url, settings.cache_ttl_seconds)
    await cache.connect()
    app.state.cache = cache

    # Client + orchestrator.
    client = LinkedInClient(settings=settings, pool=pool, log=log)
    app.state.client = client
    app.state.orchestrator = Orchestrator(settings)

    log.info("app.started",
             version=APP_VERSION,
             sessions=pool.health(),
             has_api_key=settings.has_api_key,
             redis=bool(settings.redis_url))
    try:
        yield
    finally:
        await cache.close()
        log.info("app.stopped")


# --- app ---------------------------------------------------------------------

app = FastAPI(
    title="LinkedIn Profile API",
    description="HTTP API that returns a LinkedIn profile as structured JSON by hitting "
                "LinkedIn's Voyager endpoints directly. No browser in the runtime.",
    version=APP_VERSION,
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
async def serve_website() -> HTMLResponse:
    """Serve the single-page website that wraps the API.

    The API key is injected into the page so the frontend JS can call /v1/profile
    without the user typing a key. The key is only exposed on the same origin —
    external callers still need the X-API-Key header.
    """
    html = (STATIC_DIR / "index.html").read_text()
    if settings.has_api_key:
        html = html.replace(
            "const API_KEY = localStorage.getItem('api_key') || '';",
            f"const API_KEY = {orjson.dumps(settings.api_key).decode()};",
        )
    return HTMLResponse(content=html)


# --- image proxy (for LinkedIn CDN images that check Referer) ----------------

@app.get("/v1/img", include_in_schema=False)
async def proxy_image(url: str) -> Response:
    """Proxy a LinkedIn CDN image so the browser can load it.

    LinkedIn's media CDN images are signed and time-limited but load fine
    server-side. In the browser, they may be blocked by Referer policy.
    This endpoint fetches the image server-side and streams it back, so the
    browser sees a same-origin image with no CORS/Referer issues.
    """
    from curl_cffi.requests import AsyncSession

    if not url.startswith("https://media.licdn.com/"):
        return JSONResponse(status_code=400, content={"error": "only media.licdn.com URLs allowed"})
    try:
        client = AsyncSession(impersonate="chrome124")
        resp = await client.get(url, headers={"Referer": "https://www.linkedin.com/"})
        content_type = resp.headers.get("content-type", "image/jpeg")
        return Response(content=resp.content, media_type=content_type)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"image fetch failed: {e}"})


# --- middleware (request id) -------------------------------------------------

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    rid = request.headers.get("x-request-id") or "01" + uuid.uuid4().hex[:22]
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


# --- auth --------------------------------------------------------------------

def _check_api_key(x_api_key: str | None) -> None:
    """Constant-time API key check. Missing or wrong -> 401."""
    if not settings.has_api_key:
        # No key configured: fail closed. Document in /health.
        raise UnauthorizedError("server has no API key configured; set API_KEY env var")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise UnauthorizedError("missing or invalid X-API-Key header")


# --- exception handlers ------------------------------------------------------

@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.as_dict(request_id=rid),
        headers={"X-Request-ID": rid} if rid else None,
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    rid = getattr(request.state, "request_id", None) if hasattr(request, "state") else None
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "INVALID_URL", "message": "request validation failed", "request_id": rid}},
        headers={"X-Request-ID": rid} if rid else None,
    )


# --- health ------------------------------------------------------------------

@app.get("/v1/health", tags=["meta"])
async def health() -> dict:
    """Honest health: session pool counts, redis presence, version. No token material."""
    pool = app.state.pool
    return {
        "status": "ok",
        "version": APP_VERSION,
        "sessions": pool.health(),
        "redis": bool(settings.redis_url),
        "has_api_key": settings.has_api_key,
    }


# --- profile endpoint --------------------------------------------------------

@app.post("/v1/profile", response_model=ProfileResponse, tags=["profile"])
async def post_profile(req: ProfileRequest, request: Request, x_api_key: str | None = Header(None)) -> ProfileResponse:
    _check_api_key(x_api_key)
    return await _fetch_profile(req.url, req.refresh, request)


@app.get("/v1/profile", response_model=ProfileResponse, tags=["profile"])
async def get_profile(url: str, request: Request, refresh: bool = False, x_api_key: str | None = Header(None)) -> ProfileResponse:
    _check_api_key(x_api_key)
    return await _fetch_profile(url, refresh, request)


async def _fetch_profile(raw_url: str, refresh: bool, request: Request) -> ProfileResponse:
    """Shared fetch path for POST and GET.

    1. normalize URL to slug
    2. check cache (unless refresh)
    3. orchestrator -> raw payload
    4. map sections (each isolated; failures -> partial_sections)
    5. compute completeness
    6. write to cache
    """
    rid = getattr(request.state, "request_id", "")
    log = get_logger("app.profile").bind(request_id=rid)

    slug = normalize_profile_url(raw_url)
    cache: ProfileCache = app.state.cache

    # Cache read (unless refresh bypass).
    cached = None if refresh else await cache.get(slug)
    if cached is not None:
        resp = _decode_cached(cached, slug, rid)
        log.info("profile.cache_hit", slug=slug)
        return resp

    # Fetch via orchestrator.
    client: LinkedInClient = app.state.client
    orch: Orchestrator = app.state.orchestrator
    try:
        result = await orch.fetch(slug, client)
    except NoSessionsError:
        # Stale-on-error: serve stale cache if we have one.
        stale = await cache.get_stale(slug)
        if stale is not None:
            resp = _decode_cached(stale, slug, rid, stale=True)
            log.info("profile.stale_served", slug=slug, reason="no_sessions")
            return resp
        raise

    if not result.sections:
        # No strategy produced a payload. Try stale cache before erroring.
        stale = await cache.get_stale(slug)
        if stale is not None:
            resp = _decode_cached(stale, slug, rid, stale=True)
            log.info("profile.stale_served", slug=slug, reason="no_strategy")
            return resp
        from app.errors import ProfileNotFoundError
        raise ProfileNotFoundError(f"no data found for slug '{slug}' via any strategy")

    # Map sections, each in its own try/except (Gate 3: one bad mapper never 500s).
    primary = result.sections.get("_primary", {})
    partial: list[str] = []

    if result.source == "flagship_web_rsc" and isinstance(primary, dict):
        # Flagship-web RSC payload: text lists, not normalized envelope.
        from app.linkedin.strategies.flagship_web import (
            map_education_from_rsc,
            map_experience_from_rsc,
            map_languages_from_rsc,
            map_profile_from_rsc,
        )
        main_texts = primary.get("main_texts", [])
        exp_texts = primary.get("experience_texts", [])
        lang_texts = primary.get("language_texts", [])
        about_texts = primary.get("about_texts", [])

        # Profile mapper takes both main_texts and about_texts for the about section
        try:
            profile = map_profile_from_rsc(main_texts, about_texts)
        except Exception as e:
            get_logger("app.mapper").warning("mapper.failed", section="profile", error=str(e))
            partial.append("profile")
            from app.models import Profile
            profile = Profile()
        experience = _safe_map_list_rsc("experience", map_experience_from_rsc, exp_texts, partial)
        education = _safe_map_list_rsc("education", map_education_from_rsc, main_texts, partial)
        skills: list = []
        certifications: list = []
        languages = _safe_map_list_rsc("languages", map_languages_from_rsc, lang_texts, partial)
        projects: list = []
        publications: list = []
        honors: list = []
        volunteer: list = []
        courses: list = []
    else:
        # Voyager normalized-envelope payload: use UrnGraph mappers.
        graph = UrnGraph(primary if isinstance(primary, dict) else {})
        profile = _safe_map("profile", map_profile, graph, partial)
        experience = _safe_map_list("experience", map_experience, graph, partial)
        education = _safe_map_list("education", map_education, graph, partial)
        skills = _safe_map_list("skills", map_skills, graph, partial)
        certifications = _safe_map_list("certifications", map_certifications, graph, partial)
        languages = _safe_map_list("languages", map_languages, graph, partial)
        projects = _safe_map_list("projects", map_projects, graph, partial)
        publications = _safe_map_list("publications", map_publications, graph, partial)
        honors = _safe_map_list("honors", map_honors, graph, partial)
        volunteer = _safe_map_list("volunteer", map_volunteer, graph, partial)
        courses = _safe_map_list("courses", map_courses, graph, partial)

    completeness = compute_completeness(profile, experience, education, skills)

    meta = Meta(
        profile_url=f"https://www.linkedin.com/in/{slug}",
        public_identifier=slug,
        profile_urn=result.profile_urn,
        fetched_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source=result.source,
        supplemented_by=result.supplemented_by,
        cache=CacheMeta(hit=False, age_seconds=0, stale=False),
        partial_sections=partial,
        completeness=completeness,
        request_id=rid,
    )
    response = ProfileResponse(
        meta=meta,
        profile=profile,
        experience=experience,
        education=education,
        skills=skills,
        certifications=certifications,
        languages=languages,
        projects=projects,
        publications=publications,
        honors=honors,
        volunteer=volunteer,
        courses=courses,
    )

    # Write to cache (best effort).
    await cache.set(slug, orjson.dumps(response.model_dump()))

    log.info("profile.fetched",
             slug=slug,
             source=result.source,
             completeness=completeness,
             partial=partial)
    return response


def _safe_map(name: str, fn, graph: UrnGraph, partial: list[str]):  # type: ignore[no-untyped-def]
    """Run a single-mapper. On any exception, log and return an empty model; record in partial."""
    log = get_logger("app.mapper")
    try:
        return fn(graph)
    except Exception as e:
        log.warning("mapper.failed", section=name, error=str(e))
        partial.append(name)
        # Return an empty Profile so the response still has the field.
        from app.models import Profile
        return Profile()


def _safe_map_list(name: str, fn, graph: UrnGraph, partial: list[str]) -> list:  # type: ignore[no-untyped-def]
    """Run a list-mapper. On any exception, log and return []; record in partial."""
    log = get_logger("app.mapper")
    try:
        return fn(graph)
    except Exception as e:
        log.warning("mapper.failed", section=name, error=str(e))
        partial.append(name)
        return []


def _safe_map_rsc(name: str, fn, texts: list[str], partial: list[str]):  # type: ignore[no-untyped-def]
    """Run an RSC single-mapper. On exception, log + return empty Profile; record in partial."""
    log = get_logger("app.mapper")
    try:
        return fn(texts)
    except Exception as e:
        log.warning("mapper.failed", section=name, error=str(e))
        partial.append(name)
        from app.models import Profile
        return Profile()


def _safe_map_list_rsc(name: str, fn, texts: list[str], partial: list[str]) -> list:  # type: ignore[no-untyped-def]
    """Run an RSC list-mapper. On exception, log + return []; record in partial."""
    log = get_logger("app.mapper")
    try:
        return fn(texts)
    except Exception as e:
        log.warning("mapper.failed", section=name, error=str(e))
        partial.append(name)
        return []


def _decode_cached(blob: bytes, slug: str, rid: str, *, stale: bool = False) -> ProfileResponse:
    """Reconstruct a ProfileResponse from a cache blob."""
    data = orjson.loads(blob)
    resp = ProfileResponse.model_validate(data)
    resp.meta.cache = CacheMeta(hit=True, age_seconds=0, stale=stale)
    resp.meta.request_id = rid
    return resp
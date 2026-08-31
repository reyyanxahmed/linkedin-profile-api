"""Application configuration loaded from environment via pydantic-settings.

Single responsibility: parse and validate env vars into typed settings. No I/O, no
side effects beyond reading os.environ. If LI_SESSIONS is empty the app still boots —
authenticated strategies are skipped and the public HTML fallback remains available.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cookie domains, as a real browser scopes them. The domain is load-bearing: curl
# only replays a cookie on the 302 datacenter-affinity hop if the scope matches, and
# a mis-scoped li_at produces an endless self-redirect instead of a 401. Values here
# match a Chrome cookie export from linkedin.com.
COOKIE_DOMAINS: dict[str, str] = {
    "li_at": ".www.linkedin.com",
    "JSESSIONID": ".www.linkedin.com",
    "bscookie": ".www.linkedin.com",
    "li_theme": ".www.linkedin.com",
    "li_theme_set": ".www.linkedin.com",
    "timezone": ".www.linkedin.com",
    "bcookie": ".linkedin.com",
    "lidc": ".linkedin.com",
    "liap": ".linkedin.com",
    "lang": ".linkedin.com",
    "dfpfpt": ".linkedin.com",
    "__cf_bm": ".linkedin.com",
    "UserMatchHistory": ".linkedin.com",
    "fptctx2": ".linkedin.com",
}
DEFAULT_COOKIE_DOMAIN = ".www.linkedin.com"


@dataclass
class Cookie:
    """One cookie, with the domain scope it must be replayed under."""

    name: str
    value: str
    domain: str


@dataclass
class SessionConfig:
    """One LinkedIn session.

    `cookies` is the full browser cookie set. LinkedIn's edge needs more than
    li_at+JSESSIONID: `lidc` pins the datacenter and `bcookie`/`__cf_bm` clear bot
    management. Sending only the two auth cookies makes every identity endpoint
    self-redirect forever.

    `li_at` and `jsessionid` remain available as derived properties so existing
    callers and tests keep working.
    """

    cookies: list[Cookie]

    @property
    def li_at(self) -> str:
        return self._get("li_at")

    @property
    def jsessionid(self) -> str:
        # Stored WITHOUT surrounding quotes; the cookie value carries them, the
        # csrf-token header must not.
        return self._get("JSESSIONID").strip('"')

    def _get(self, name: str) -> str:
        for c in self.cookies:
            if c.name == name:
                return c.value
        return ""


def _cookies_from_export(items: list) -> list[Cookie]:
    """Build a cookie list from a browser cookie-export array.

    Accepts the shape Chrome/EditThisCookie emit: objects with name/value and
    optionally domain. Unknown cookies keep whatever domain the export gives, so a
    future LinkedIn cookie works without a code change.
    """
    out: list[Cookie] = []
    for it in items:
        if not isinstance(it, dict) or "name" not in it or "value" not in it:
            continue
        name = str(it["name"])
        domain = str(it.get("domain") or COOKIE_DOMAINS.get(name, DEFAULT_COOKIE_DOMAIN))
        out.append(Cookie(name=name, value=str(it["value"]), domain=domain))
    return out


class Settings(BaseSettings):
    """All runtime config. Defaults match .env.example and BUILD_SPEC.md section 3."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_key: str = Field(default="", alias="API_KEY")
    li_sessions_raw: str = Field(default="", alias="LI_SESSIONS")
    # Empty by default: no Redis attempt unless one is actually configured. A
    # localhost default costs a connect-timeout on every cold start of a serverless
    # deployment that has no Redis, for a cache that then falls back to memory anyway.
    redis_url: str = Field(default="", alias="REDIS_URL")
    cache_ttl_seconds: int = Field(default=86400, alias="CACHE_TTL_SECONDS")
    impersonate: str = Field(default="chrome150", alias="IMPERSONATE")
    min_delay_ms: int = Field(default=800, alias="MIN_DELAY_MS")
    max_delay_ms: int = Field(default=2500, alias="MAX_DELAY_MS")
    max_concurrency: int = Field(default=2, alias="MAX_CONCURRENCY")
    session_cooldown_seconds: int = Field(default=900, alias="SESSION_COOLDOWN_SECONDS")
    http_proxy_url: str = Field(default="", alias="HTTP_PROXY_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    offline_mode: bool = Field(default=False, alias="OFFLINE_MODE")
    fixture_dir: str = Field(default="", alias="FIXTURE_DIR")
    # Where rotated cookies are persisted. LinkedIn rotates li_at during use, so the
    # value in LI_SESSIONS goes stale; this file holds the current one across
    # restarts. Contains credentials — gitignored, written 0600. Blank disables it.
    cookie_state_path: str = Field(default=".cookie_state.json", alias="COOKIE_STATE_PATH")
    # Serve /v1/profile without an X-API-Key. Off by default: with no API_KEY set the
    # API fails closed, which is the right default for a credentialed scraper. The
    # public demo deployment turns this ON deliberately so reviewers can exercise the
    # API from a browser. Setting API_KEY always re-enables enforcement, even here.
    allow_unauthenticated: bool = Field(default=False, alias="ALLOW_UNAUTHENTICATED")

    @field_validator("li_sessions_raw")
    @classmethod
    def _parse_sessions(cls, v: str) -> str:
        # Store raw; sessions property does the typed parse so a bad value surfaces
        # at access time with a clear error rather than crashing boot.
        return v

    @property
    def sessions(self) -> list[SessionConfig]:
        """Parsed session list. Empty if LI_SESSIONS unset/blank. Raises ValueError on malformed JSON."""
        raw = self.li_sessions_raw.strip()
        if not raw:
            return []
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"LI_SESSIONS is not valid JSON: {e}") from e
        if not isinstance(data, list):
            raise ValueError("LI_SESSIONS must be a JSON array of sessions")
        out: list[SessionConfig] = []
        for i, item in enumerate(data):
            cookies = _parse_session_item(i, item)
            if not any(c.name == "li_at" for c in cookies):
                raise ValueError(f"LI_SESSIONS[{i}] has no li_at cookie")
            out.append(SessionConfig(cookies=cookies))
        return out

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key.strip())


def _parse_session_item(index: int, item: Any) -> list[Cookie]:
    """Parse one LI_SESSIONS entry into a cookie list.

    Three accepted shapes, so a browser export can be pasted in unedited:
      1. {"li_at": "...", "jsessionid": "..."}   — the original minimal form
      2. {"cookies": [ {name, value, domain}, ... ]}
      3. [ {name, value, domain}, ... ]          — a raw browser cookie export
    """
    if isinstance(item, list):
        return _cookies_from_export(item)

    if isinstance(item, dict) and isinstance(item.get("cookies"), list):
        return _cookies_from_export(item["cookies"])

    if isinstance(item, dict) and "li_at" in item and "jsessionid" in item:
        # Minimal form. JSESSIONID is stored bare in config but the cookie value
        # LinkedIn expects is quoted, so re-add the quotes here.
        js = str(item["jsessionid"]).strip().strip('"')
        return [
            Cookie("li_at", str(item["li_at"]), COOKIE_DOMAINS["li_at"]),
            Cookie("JSESSIONID", f'"{js}"', COOKIE_DOMAINS["JSESSIONID"]),
        ]

    raise ValueError(
        f"LI_SESSIONS[{index}] must be a cookie export array, a {{cookies: [...]}} "
        f"object, or {{li_at, jsessionid}}; got: {type(item).__name__}"
    )


def get_settings() -> Settings:
    """Construct settings from os.environ. Cheap; safe to call repeatedly."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
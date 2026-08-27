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


@dataclass
class SessionConfig:
    """One LinkedIn session. jsessionid is stored WITHOUT surrounding quotes."""

    li_at: str
    jsessionid: str


class Settings(BaseSettings):
    """All runtime config. Defaults match .env.example and BUILD_SPEC.md section 3."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_key: str = Field(default="", alias="API_KEY")
    li_sessions_raw: str = Field(default="", alias="LI_SESSIONS")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    cache_ttl_seconds: int = Field(default=86400, alias="CACHE_TTL_SECONDS")
    impersonate: str = Field(default="chrome124", alias="IMPERSONATE")
    min_delay_ms: int = Field(default=800, alias="MIN_DELAY_MS")
    max_delay_ms: int = Field(default=2500, alias="MAX_DELAY_MS")
    max_concurrency: int = Field(default=2, alias="MAX_CONCURRENCY")
    session_cooldown_seconds: int = Field(default=900, alias="SESSION_COOLDOWN_SECONDS")
    http_proxy_url: str = Field(default="", alias="HTTP_PROXY_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

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
            raise ValueError("LI_SESSIONS must be a JSON array of {li_at, jsessionid} objects")
        out: list[SessionConfig] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict) or "li_at" not in item or "jsessionid" not in item:
                raise ValueError(
                    f"LI_SESSIONS[{i}] must have 'li_at' and 'jsessionid' keys; got: {item!r}"
                )
            # Strip surrounding quotes from jsessionid if present — cookie value has them,
            # config stores the bare token.
            js: str = str(item["jsessionid"]).strip().strip('"')
            out.append(SessionConfig(li_at=str(item["li_at"]), jsessionid=js))
        return out

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key.strip())


def get_settings() -> Settings:
    """Construct settings from os.environ. Cheap; safe to call repeatedly."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
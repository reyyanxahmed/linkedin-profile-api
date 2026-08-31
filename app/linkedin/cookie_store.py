"""Persistence for rotating LinkedIn cookies.

Single responsibility: read and write the current cookie state for each session to
a local JSON file, so a rotated `li_at` survives a process restart.

Why this exists: LinkedIn rotates `li_at` during normal use. Each response can carry
a `Set-Cookie` with a fresh value, and the previous value stops authenticating
shortly after. A client that keeps replaying the value from `.env` therefore works
for a request or two and then gets 401 on everything — which looks exactly like a
dead account, but is not. Holding the rotated value is what keeps a session alive.

The state file contains live credentials. It is gitignored, written 0600, and never
logged. No PII beyond the cookies themselves.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import structlog

from app.config import Cookie

log = structlog.get_logger("cookie_store")


def _key(jsessionid: str) -> str:
    """Stable per-session key. JSESSIONID is stable across li_at rotations."""
    return jsessionid.strip('"')


def load(path: str) -> dict[str, list[Cookie]]:
    """Load persisted cookie state. Returns {} when the file is absent or corrupt.

    A corrupt state file must never stop the app booting — the configured cookies
    from LI_SESSIONS remain a valid starting point.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cookie_store.unreadable", path=path, error=str(e))
        return {}
    if not isinstance(raw, dict):
        log.warning("cookie_store.unexpected_shape", path=path)
        return {}
    if not raw:
        # The file exists but holds nothing. Distinguished from "absent" in the log
        # because an empty store means sessions silently fall back to LI_SESSIONS.
        log.warning("cookie_store.empty", path=path)
        return {}

    out: dict[str, list[Cookie]] = {}
    for key, items in raw.items():
        if not isinstance(items, list):
            continue
        out[key] = [
            Cookie(str(i["name"]), str(i["value"]), str(i["domain"]))
            for i in items
            if isinstance(i, dict) and {"name", "value", "domain"} <= i.keys()
        ]
    log.info("cookie_store.loaded", sessions=len(out))
    return out


def save(path: str, sessions: list) -> None:
    """Persist every session's current cookie set.

    Written atomically via a temp file in the same directory, then renamed, so a
    crash mid-write cannot leave a truncated state file behind.
    """
    if not path:
        return
    payload = {
        _key(s.jsessionid): [
            {"name": c.name, "value": c.value, "domain": c.domain} for c in s.cookie_list()
        ]
        for s in sessions
        if s.jsessionid
    }
    if not payload:
        # Refuse to truncate the store to nothing. Overwriting a good state file with
        # {} silently reverts every session to the stale credentials in LI_SESSIONS,
        # which then 401 — a failure that looks like a dead account, not a lost file.
        # Having nothing to persist is a no-op, never a delete.
        if sessions:
            log.warning("cookie_store.refusing_empty_write", sessions=len(sessions))
        return
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".cookies-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except BaseException:
            # Never leave the temp file behind on any failure path, including
            # KeyboardInterrupt during a write.
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        # A read-only filesystem (some container setups) must not break fetching.
        # The session still works in-process; it just will not survive a restart.
        log.warning("cookie_store.unwritable", path=path, error=str(e))


def apply(sessions: list, state: dict[str, list[Cookie]]) -> int:
    """Overlay persisted cookies onto sessions. Returns how many were updated.

    Persisted state wins over LI_SESSIONS: it is strictly newer, because it only
    ever gets written after LinkedIn handed us a rotated value.
    """
    updated = 0
    for s in sessions:
        stored = state.get(_key(s.jsessionid))
        if not stored:
            continue
        s.cookies = list(stored)
        for c in stored:
            if c.name == "li_at":
                s.li_at = c.value
        updated += 1
    return updated

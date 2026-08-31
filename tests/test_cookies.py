"""Cookie handling: config shapes, rotation, and persistence.

These tests encode the single most expensive lesson in this project: LinkedIn's
auth is not a static pair of cookies. `li_at` rotates during normal use, and the
edge needs `lidc`/`bcookie` alongside it. Getting either wrong produces failures
that look like a banned account but are not.

Entirely offline — no network, no fixtures needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Cookie, Settings, _parse_session_item
from app.linkedin import cookie_store
from app.linkedin.session import Session, SessionPool


class TestSessionConfigShapes:
    """LI_SESSIONS accepts a pasted browser export, not just the minimal pair."""

    def test_minimal_pair(self) -> None:
        cookies = _parse_session_item(0, {"li_at": "tok", "jsessionid": "ajax:1"})
        by_name = {c.name: c for c in cookies}
        assert by_name["li_at"].value == "tok"
        # The cookie value is quoted even though config stores the token bare.
        assert by_name["JSESSIONID"].value == '"ajax:1"'
        # Domains are the ones a real browser uses; this is load-bearing for redirects.
        assert by_name["li_at"].domain == ".www.linkedin.com"

    def test_minimal_pair_tolerates_quoted_jsessionid(self) -> None:
        cookies = _parse_session_item(0, {"li_at": "tok", "jsessionid": '"ajax:1"'})
        by_name = {c.name: c.value for c in cookies}
        assert by_name["JSESSIONID"] == '"ajax:1"'

    def test_raw_browser_export(self) -> None:
        export = [
            {"name": "li_at", "value": "tok", "domain": ".www.linkedin.com"},
            {"name": "lidc", "value": '"b=OGST00"', "domain": ".linkedin.com"},
        ]
        cookies = _parse_session_item(0, export)
        assert {c.name for c in cookies} == {"li_at", "lidc"}
        assert next(c for c in cookies if c.name == "lidc").domain == ".linkedin.com"

    def test_cookies_wrapper_object(self) -> None:
        item = {"cookies": [{"name": "li_at", "value": "tok"}]}
        cookies = _parse_session_item(0, item)
        assert cookies[0].name == "li_at"
        # Domain falls back to the known map when the export omits it.
        assert cookies[0].domain == ".www.linkedin.com"

    def test_unknown_cookie_keeps_its_export_domain(self) -> None:
        """A cookie LinkedIn adds later must work without a code change."""
        cookies = _parse_session_item(0, [{"name": "li_new", "value": "v", "domain": ".example"}])
        assert cookies[0].domain == ".example"

    def test_garbage_item_raises(self) -> None:
        with pytest.raises(ValueError, match="LI_SESSIONS\\[3\\]"):
            _parse_session_item(3, "not-a-session")

    def test_session_without_li_at_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LI_SESSIONS", json.dumps([[{"name": "lidc", "value": "x"}]]))
        with pytest.raises(ValueError, match="no li_at"):
            _ = Settings(_env_file=None).sessions  # type: ignore[call-arg]

    def test_empty_is_no_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LI_SESSIONS", "")
        assert Settings(_env_file=None).sessions == []  # type: ignore[call-arg]


class TestCookieRotation:
    def test_rotated_li_at_is_adopted(self) -> None:
        s = Session(li_at="old", jsessionid="ajax:1")
        assert s.update_from_jar({"li_at": "new"}) is True
        assert s.li_at == "new"
        assert {c.name: c.value for c in s.cookie_list()}["li_at"] == "new"

    def test_unchanged_jar_reports_no_change(self) -> None:
        """No change means no disk write on the hot path."""
        s = Session(li_at="tok", jsessionid="ajax:1", cookies=[Cookie("li_at", "tok", ".d")])
        assert s.update_from_jar({"li_at": "tok"}) is False

    def test_new_cookie_is_appended(self) -> None:
        s = Session(li_at="tok", jsessionid="ajax:1", cookies=[Cookie("li_at", "tok", ".d")])
        assert s.update_from_jar({"lidc": '"b=OGST00"'}) is True
        assert {c.name for c in s.cookies} == {"li_at", "lidc"}

    def test_empty_values_are_ignored(self) -> None:
        """Cleared cookies must not blank out a working credential."""
        s = Session(li_at="tok", jsessionid="ajax:1", cookies=[Cookie("li_at", "tok", ".d")])
        assert s.update_from_jar({"li_at": ""}) is False
        assert s.li_at == "tok"


class TestCookieStore:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = str(tmp_path / "state.json")
        s = Session(li_at="rotated", jsessionid="ajax:1", cookies=[Cookie("li_at", "rotated", ".d")])
        cookie_store.save(path, [s])

        fresh = Session(li_at="stale", jsessionid="ajax:1")
        assert cookie_store.apply([fresh], cookie_store.load(path)) == 1
        # Persisted state wins: it is strictly newer than whatever LI_SESSIONS holds.
        assert fresh.li_at == "rotated"

    def test_state_file_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        cookie_store.save(str(path), [Session(li_at="t", jsessionid="ajax:1")])
        assert path.stat().st_mode & 0o077 == 0, "state file holds live credentials"

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert cookie_store.load(str(tmp_path / "nope.json")) == {}

    def test_corrupt_file_does_not_break_boot(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("{ not json")
        assert cookie_store.load(str(path)) == {}

    def test_disabled_when_path_blank(self, tmp_path: Path) -> None:
        cookie_store.save("", [Session(li_at="t", jsessionid="ajax:1")])
        assert cookie_store.load("") == {}
        assert list(tmp_path.iterdir()) == []

    def test_no_temp_files_left_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        cookie_store.save(str(path), [Session(li_at="t", jsessionid="ajax:1")])
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_apply_ignores_unknown_sessions(self) -> None:
        s = Session(li_at="stale", jsessionid="ajax:other")
        state = {"ajax:1": [Cookie("li_at", "rotated", ".d")]}
        assert cookie_store.apply([s], state) == 0
        assert s.li_at == "stale"


class TestPoolFromConfig:
    def test_pool_carries_full_cookie_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        export = [[
            {"name": "li_at", "value": "tok", "domain": ".www.linkedin.com"},
            {"name": "JSESSIONID", "value": '"ajax:9"', "domain": ".www.linkedin.com"},
            {"name": "lidc", "value": '"b=OGST00"', "domain": ".linkedin.com"},
        ]]
        monkeypatch.setenv("LI_SESSIONS", json.dumps(export))
        pool = SessionPool.from_raw(Settings(_env_file=None).sessions)  # type: ignore[call-arg]
        s = pool.acquire()
        assert s.li_at == "tok"
        assert s.jsessionid == "ajax:9"  # quotes stripped for the csrf-token header
        # lidc must survive into the jar — without it every identity endpoint
        # self-redirects until curl aborts.
        assert "lidc" in {c.name for c in s.cookie_list()}


class TestOfflineModeIsHard:
    """OFFLINE_MODE must open no sockets, not merely prefer fixtures."""

    def test_chain_is_fixtures_only(self) -> None:
        from app.linkedin.orchestrator import build_strategies
        from app.linkedin.strategies.flagship_web import FlagshipWebStrategy

        chain = build_strategies({}, offline_mode=True, fixture_dir="tests/fixtures/rsc")
        assert [type(s) for s in chain] == [FlagshipWebStrategy]
        assert chain[0].offline_mode is True

    def test_online_chain_keeps_the_fallbacks(self) -> None:
        chain = build_strategies_online()
        assert len(chain) > 1, "the fallback chain must survive outside offline mode"


def build_strategies_online() -> list:
    from app.linkedin.orchestrator import build_strategies

    return build_strategies({}, offline_mode=False, fixture_dir="")


class TestEnvelopeErrors:
    """LinkedIn hides real failures inside a 200-shaped Rest.li envelope."""

    def test_gone_envelope_is_detected(self) -> None:
        from app.linkedin.client import envelope_status

        assert envelope_status(b'{"data":{"status":410},"included":[]}') == 410

    def test_real_payload_is_not_an_error(self) -> None:
        from app.linkedin.client import envelope_status

        body = b'{"data":{"status":"ACTIVE","firstName":"Ada"},"included":[]}'
        assert envelope_status(body) is None

    def test_payload_with_included_is_not_an_error(self) -> None:
        from app.linkedin.client import envelope_status

        assert envelope_status(b'{"data":{"status":410},"included":[{"a":1}]}') is None

    def test_large_body_is_skipped(self) -> None:
        from app.linkedin.client import envelope_status

        assert envelope_status(b'{"data":{"status":410}}' + b" " * 600) is None

    def test_garbage_body_is_safe(self) -> None:
        from app.linkedin.client import envelope_status

        assert envelope_status(b"<html>nope") is None
        assert envelope_status(b"") is None

    def test_410_classifies_as_not_found(self) -> None:
        from app.linkedin.client import Outcome, classify

        # A retired sub-resource must not cool a healthy session.
        assert classify(410, "application/json", ord("{"), "https://x/voyager/api/x") is Outcome.NOT_FOUND


class TestCookieStoreNeverTruncates:
    def test_refuses_to_write_empty_over_good_state(self, tmp_path: Path) -> None:
        """A bad write must not revert sessions to the stale LI_SESSIONS values."""
        path = tmp_path / "state.json"
        good = Session(li_at="rotated", jsessionid="ajax:1")
        cookie_store.save(str(path), [good])

        # A session with no jsessionid produces no key, so the payload would be {}.
        cookie_store.save(str(path), [Session(li_at="x", jsessionid="")])

        assert list(cookie_store.load(str(path))) == ["ajax:1"]

    def test_genuinely_empty_session_list_is_a_no_op(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        cookie_store.save(str(path), [Session(li_at="t", jsessionid="ajax:1")])
        cookie_store.save(str(path), [])
        assert list(cookie_store.load(str(path))) == ["ajax:1"]


class TestAuthGating:
    """A configured API_KEY must always win over the open-access flag."""

    def _settings(self, **env: str):
        from app.config import Settings

        return Settings(_env_file=None, **env)  # type: ignore[call-arg,arg-type]

    def test_configured_key_is_enforced_even_when_open_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.main as m
        from app.errors import UnauthorizedError

        monkeypatch.setattr(m, "settings", self._settings(API_KEY="k", ALLOW_UNAUTHENTICATED="true"))
        # The flag must never silently disable a key someone deliberately set.
        with pytest.raises(UnauthorizedError):
            m._check_api_key(None)
        m._check_api_key("k")  # correct key still works

    def test_open_flag_allows_access_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.main as m

        monkeypatch.setattr(m, "settings", self._settings(API_KEY="", ALLOW_UNAUTHENTICATED="true"))
        m._check_api_key(None)  # must not raise

    def test_fails_closed_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.main as m
        from app.errors import UnauthorizedError

        monkeypatch.setattr(m, "settings", self._settings(API_KEY="", ALLOW_UNAUTHENTICATED="false"))
        with pytest.raises(UnauthorizedError):
            m._check_api_key(None)

"""Tests for the flagship-web RSC parser and strategy mappers.

These tests use real (redacted) fixtures captured from LinkedIn's flagship-web RSC
transport. They verify that the RSC parser correctly extracts text from the base64-
encoded SDUI wire format, and that the mappers produce structured profile data.

Fixtures in tests/fixtures/rsc/:
  - main_profile_barackobama.json: main page RSC stream for barackobama
  - experience_barackobama.json: experience section RSC stream
  - languages_barackobama.json: languages section RSC stream
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.linkedin.rsc_parser import decode_rsc, extract_text, parse_rsc_lines
from app.linkedin.strategies.flagship_web import (
    map_education_from_rsc,
    map_experience_from_rsc,
    map_languages_from_rsc,
    map_profile_from_rsc,
)

FIXTURE_DIR = Path("tests/fixtures/rsc")


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


def _extract(name: str, key: str = "body") -> list[str]:
    fixture = _load_fixture(name)
    return extract_text(fixture[key])


# --- RSC parser unit tests ---------------------------------------------------

class TestRscParser:
    def test_decode_base64(self) -> None:
        # "Hello" in base64
        assert decode_rsc("SGVsbG8=") == "Hello"

    def test_decode_passthrough_non_base64(self) -> None:
        # Non-base64 text passes through
        assert decode_rsc("not base64!") == "not base64!"

    def test_extract_text_from_empty(self) -> None:
        assert extract_text("") == []

    def test_parse_rsc_lines_returns_arrays(self) -> None:
        # A minimal RSC stream with one component array line
        rsc = '0:["$","div",null,{"children":["Hello"]}]'
        payloads = parse_rsc_lines(rsc)
        assert len(payloads) == 1
        assert isinstance(payloads[0], list)

    def test_extract_text_finds_content(self) -> None:
        rsc = '0:["$","div",null,{"children":["Hello World"]}]'
        texts = extract_text(rsc)
        assert "Hello World" in texts

    def test_extract_text_skips_css_and_metadata(self) -> None:
        rsc = '0:["$","div",null,{"className":"_abc123","style":{"color":"red"},"children":["Real Content"]}]'
        texts = extract_text(rsc)
        assert "Real Content" in texts
        assert "_abc123" not in texts
        assert "red" not in texts


# --- Profile mapper tests (from fixture) -------------------------------------

@pytest.fixture
def main_texts() -> list[str]:
    return _extract("main_profile_barackobama.json")


@pytest.fixture
def exp_texts() -> list[str]:
    return _extract("experience_barackobama.json")


@pytest.fixture
def lang_texts() -> list[str]:
    return _extract("languages_barackobama.json")


class TestProfileFromRsc:
    def test_name(self, main_texts: list[str]) -> None:
        p = map_profile_from_rsc(main_texts)
        assert p.full_name == "Barack Obama"
        assert p.first_name == "Barack"
        assert p.last_name == "Obama"

    def test_headline(self, main_texts: list[str]) -> None:
        p = map_profile_from_rsc(main_texts)
        assert p.headline is not None
        assert "President" in p.headline

    def test_profile_urn(self, main_texts: list[str]) -> None:
        # The mapper should not crash; URN extraction is in the strategy.
        p = map_profile_from_rsc(main_texts)
        assert p is not None

    def test_images_extracted(self, main_texts: list[str]) -> None:
        p = map_profile_from_rsc(main_texts)
        # Barack Obama's profile should have a profile photo
        assert len(p.images.profile) >= 1 or len(p.images.background) >= 1


class TestExperienceFromRsc:
    def test_returns_list(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        assert isinstance(exps, list)
        assert len(exps) >= 2  # barackobama has at least President + Senator

    def test_president_title(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        titles = [e.title for e in exps if e.title]
        assert any("President" in t for t in titles)

    def test_senator_title(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        titles = [e.title for e in exps if e.title]
        assert any("Senator" in t for t in titles)

    def test_dates_parsed(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        # The presidency: Jan 2009 - Jan 2017
        pres = [e for e in exps if e.title and "President" in e.title]
        if pres:
            assert pres[0].start is not None
            assert pres[0].start.year == 2009
            assert pres[0].end is not None
            assert pres[0].end.year == 2017

    def test_duration_months_parsed(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        pres = [e for e in exps if e.title and "President" in e.title]
        if pres:
            assert pres[0].duration_months is not None
            assert pres[0].duration_months >= 90  # ~8 years


class TestEducationFromRsc:
    def test_school_or_empty(self, main_texts: list[str]) -> None:
        edus = map_education_from_rsc(main_texts)
        # Education may or may not be in the topcard summary; either way, no crash.
        assert isinstance(edus, list)


class TestLanguagesFromRsc:
    def test_returns_list(self, lang_texts: list[str]) -> None:
        langs = map_languages_from_rsc(lang_texts)
        assert isinstance(langs, list)

    def test_languages_or_empty(self, lang_texts: list[str]) -> None:
        # barackobama may not have languages populated; either way, no crash.
        langs = map_languages_from_rsc(lang_texts)
        assert isinstance(langs, list)


class TestEmptyPayloads:
    """Every mapper must return [] or an empty Profile on empty input (Gate 3)."""

    def test_empty_profile(self) -> None:
        p = map_profile_from_rsc([])
        assert p.full_name is None

    def test_empty_experience(self) -> None:
        assert map_experience_from_rsc([]) == []

    def test_empty_education(self) -> None:
        assert map_education_from_rsc([]) == []

    def test_empty_languages(self) -> None:
        assert map_languages_from_rsc([]) == []
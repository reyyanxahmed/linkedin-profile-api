"""Tests for the flagship-web RSC parser and strategy mappers.

These tests use real (redacted) fixtures captured from LinkedIn's flagship-web RSC
transport. They verify that the RSC parser correctly extracts text from the base64-
encoded SDUI wire format, and that the mappers produce structured profile data.

Fixtures in tests/fixtures/rsc/:
  - main_profile_jasveen.json: main page RSC stream for jasveen-kaur-kainth
  - experience_jasveen.json: experience section RSC stream
  - languages_vibhu.json: languages section RSC stream
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
    return _extract("main_profile_jasveen.json")


@pytest.fixture
def exp_texts() -> list[str]:
    return _extract("experience_jasveen.json")


@pytest.fixture
def lang_texts() -> list[str]:
    return _extract("languages_vibhu.json")


class TestProfileFromRsc:
    def test_name(self, main_texts: list[str]) -> None:
        p = map_profile_from_rsc(main_texts)
        assert p.full_name == "Jasveen Kaur Kainth"
        assert p.first_name == "Jasveen"
        assert p.last_name == "Kaur Kainth"

    def test_headline(self, main_texts: list[str]) -> None:
        p = map_profile_from_rsc(main_texts)
        assert p.headline == "Analyst at Bain and Company"

    def test_location(self, main_texts: list[str]) -> None:
        p = map_profile_from_rsc(main_texts)
        assert p.location is not None
        assert "Gurugram" in (p.location.raw or "")

    def test_profile_urn(self, main_texts: list[str]) -> None:
        # The URN extraction is in the strategy, not the mapper, but the mapper
        # should not crash on it.
        p = map_profile_from_rsc(main_texts)
        assert p is not None

    def test_images_extracted(self, main_texts: list[str]) -> None:
        p = map_profile_from_rsc(main_texts)
        assert len(p.images.profile) >= 1
        assert p.images.profile[0].url.startswith("https://media.licdn.com")

    def test_background_images(self, main_texts: list[str]) -> None:
        p = map_profile_from_rsc(main_texts)
        assert len(p.images.background) >= 1


class TestExperienceFromRsc:
    def test_returns_list(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        assert isinstance(exps, list)
        assert len(exps) >= 5  # jasveen has 7 positions

    def test_first_position_title(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        assert exps[0].title == "Analyst"

    def test_first_position_company(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        assert exps[0].company is not None
        assert exps[0].company.name == "Bain & Company"

    def test_first_position_dates(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        assert exps[0].start is not None
        assert exps[0].start.year == 2025
        assert exps[0].start.month == 7
        assert exps[0].is_current is True

    def test_first_position_employment_type(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        assert exps[0].employment_type == "Full-time"

    def test_first_position_location(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        assert exps[0].location is not None
        assert "Gurugram" in exps[0].location

    def test_first_position_location_type(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        assert exps[0].location_type == "On-site"

    def test_bdo_internship_composite(self, exp_texts: list[str]) -> None:
        """The "BDO · Internship" composite should split into company=BDO, type=Internship."""
        exps = map_experience_from_rsc(exp_texts)
        bdo = [e for e in exps if e.company and e.company.name == "BDO"]
        assert len(bdo) == 1
        assert bdo[0].title == "Intern"
        assert bdo[0].employment_type == "Internship"

    def test_duration_months_parsed(self, exp_texts: list[str]) -> None:
        exps = map_experience_from_rsc(exp_texts)
        assert exps[0].duration_months is not None
        assert exps[0].duration_months > 0


class TestEducationFromRsc:
    def test_school_name(self, main_texts: list[str]) -> None:
        edus = map_education_from_rsc(main_texts)
        assert len(edus) >= 1
        assert "Thapar" in (edus[0].school or "")


class TestLanguagesFromRsc:
    def test_english(self, lang_texts: list[str]) -> None:
        langs = map_languages_from_rsc(lang_texts)
        names = [lang.name for lang in langs if lang.name]
        assert "English" in names

    def test_hindi(self, lang_texts: list[str]) -> None:
        langs = map_languages_from_rsc(lang_texts)
        names = [lang.name for lang in langs if lang.name]
        assert "Hindi" in names

    def test_proficiency(self, lang_texts: list[str]) -> None:
        langs = map_languages_from_rsc(lang_texts)
        english = [lang for lang in langs if lang.name == "English"]
        assert len(english) == 1
        assert english[0].proficiency is not None
        assert "proficiency" in (english[0].proficiency or "").lower()


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
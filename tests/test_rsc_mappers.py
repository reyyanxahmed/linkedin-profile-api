"""RSC card mappers, calibrated against a real captured profile.

The fixture is the text stream from an actual flagship-web profile page load
(tests/fixtures/rsc/profile_rajstriver.json). These tests are the record of what
the card format really looks like, so a future deploy that changes it fails here
rather than silently returning empty sections.

Offline: the fixture is on disk, nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.linkedin.strategies.flagship_web import (
    map_certifications_from_rsc,
    map_education_from_rsc,
    map_experience_from_rsc,
)

FIXTURE = Path("tests/fixtures/rsc/profile_rajstriver.json")


@pytest.fixture(scope="module")
def card_texts() -> list[str]:
    return json.loads(FIXTURE.read_text())["payload"]["card_texts"]


class TestEducation:
    def test_both_schools_are_found(self, card_texts: list[str]) -> None:
        schools = [e.school for e in map_education_from_rsc(card_texts)]
        assert schools == [
            "Calcutta Public School",
            "Jalpaiguri Government  Engineering College",
        ]

    def test_degree_and_field_are_split(self, card_texts: list[str]) -> None:
        # "B.TECH, Information Technology" -> degree, field of study.
        btech = next(
            e for e in map_education_from_rsc(card_texts) if e.degree == "B.TECH"
        )
        assert btech.field_of_study == "Information Technology"

    def test_years_are_parsed(self, card_texts: list[str]) -> None:
        first = map_education_from_rsc(card_texts)[0]
        assert (first.start.year, first.end.year) == (2013, 2015)

    def test_no_duplicate_schools(self, card_texts: list[str]) -> None:
        schools = [e.school for e in map_education_from_rsc(card_texts)]
        assert len(schools) == len(set(schools))


class TestCertifications:
    def test_certification_is_mapped(self, card_texts: list[str]) -> None:
        certs = map_certifications_from_rsc(card_texts)
        assert len(certs) == 1
        cert = certs[0]
        assert cert.name == "Algorithmic Toolbox"
        assert cert.authority == "Coursera"
        assert cert.license_number == "RKYYD4NET8ZR"
        assert (cert.issued.year, cert.issued.month) == (2017, 10)

    def test_honors_are_not_mistaken_for_certifications(self, card_texts: list[str]) -> None:
        """The honors card renders "Issued by <org> · Jan 2019".

        A looser "^Issued " anchor pulled those in as certifications. Requiring a
        date immediately after "Issued" is what separates the two sections.
        """
        names = {c.name for c in map_certifications_from_rsc(card_texts)}
        assert "Geek of the Year" not in names
        assert "IIEST Shibpur Tech Fest " not in names


class TestExperience:
    def test_positions_are_mapped(self, card_texts: list[str]) -> None:
        got = [
            (e.title, e.company.name if e.company else None)
            for e in map_experience_from_rsc(card_texts)
        ]
        assert got == [
            ("Software Engineer III", "Google"),
            ("Founder, CEO and CTO", "takeUforward"),
            ("Software Engineer II", "Google"),
            ("Software Development Engineer", "Media.net"),
            ("Educator", "Unacademy"),
            ("Software Development Engineer Intern", "Amazon"),
        ]

    def test_grouped_roles_inherit_the_company(self, card_texts: list[str]) -> None:
        """Google is a grouped employer: its sub-roles carry no company line."""
        google = [
            e for e in map_experience_from_rsc(card_texts)
            if e.company and e.company.name == "Google"
        ]
        assert len(google) == 2
        assert all(e.title for e in google)

    def test_employment_types(self, card_texts: list[str]) -> None:
        by_title = {e.title: e.employment_type for e in map_experience_from_rsc(card_texts)}
        assert by_title["Software Development Engineer Intern"] == "Internship"
        assert by_title["Educator"] == "Part-time"

    def test_current_role_has_no_end_date(self, card_texts: list[str]) -> None:
        founder = next(
            e for e in map_experience_from_rsc(card_texts)
            if e.title == "Founder, CEO and CTO"
        )
        assert founder.is_current is True
        assert founder.end is None
        assert (founder.start.year, founder.start.month) == (2024, 8)

    def test_location_and_work_mode_are_split(self, card_texts: list[str]) -> None:
        # "Bangalore Urban, Karnataka, India · Remote"
        founder = next(
            e for e in map_experience_from_rsc(card_texts)
            if e.title == "Founder, CEO and CTO"
        )
        assert founder.location == "Bangalore Urban, Karnataka, India"
        assert founder.location_type == "Remote"

    def test_education_ranges_are_not_positions(self, card_texts: list[str]) -> None:
        """Education and experience share one pooled text stream.

        Both render a bare "2013 - 2015" date line, so the range alone cannot
        separate them — older real positions use year-only ranges too (see the
        Obama fixture). The test is whether an employer was named directly above.
        """
        titles = {e.title for e in map_experience_from_rsc(card_texts)}
        assert "ISC, Computer Science" not in titles
        assert "B.TECH, Information Technology" not in titles

    def test_prose_is_not_a_title(self, card_texts: list[str]) -> None:
        """Role descriptions can land directly above a date line."""
        titles = [e.title for e in map_experience_from_rsc(card_texts)]
        assert not any(t.endswith(".") for t in titles)
        assert not any(len(t) > 90 for t in titles)


class TestEmptyInput:
    """Every card mapper must tolerate a missing or empty stream."""

    @pytest.mark.parametrize(
        "mapper",
        [map_education_from_rsc, map_certifications_from_rsc, map_experience_from_rsc],
    )
    def test_empty_texts(self, mapper) -> None:  # type: ignore[no-untyped-def]
        assert mapper([]) == []

    @pytest.mark.parametrize(
        "mapper",
        [map_education_from_rsc, map_certifications_from_rsc, map_experience_from_rsc],
    )
    def test_pure_noise(self, mapper) -> None:  # type: ignore[no-untyped-def]
        noise = ["1x", "2x", "en_US", "ProfileNullStateCardAnchor_Education", "Show all"]
        assert mapper(noise) == []


class TestLocationAttribution:
    """A position without a location must not borrow the next position's title."""

    def test_obama_positions_have_no_invented_locations(self) -> None:
        from app.linkedin.rsc_parser import extract_text

        raw = json.loads(Path("tests/fixtures/rsc/experience_barackobama.json").read_text())
        texts = extract_text(raw["body"])
        exps = map_experience_from_rsc(texts)

        # None of these entries carries a location line in the capture. Before the
        # lookahead was bounded, "President of the United States of America" was
        # assigned the location "US Senator" — the next entry's title.
        assert [e.location for e in exps] == [None, None, None, None]
        assert [e.title for e in exps] == [
            "President of the United States of America",
            "US Senator",
            "State Senator",
            "Senior Lecturer in Law",
        ]

    def test_split_media_urls_are_not_locations(self) -> None:
        """Media URLs arrive split across items; continuations have no scheme."""
        from app.linkedin.strategies.flagship_web import _is_noise

        assert _is_noise("400_400/company-logo_400_400/0/165668?e=1789603200&v=beta&t=abc")
        assert _is_noise("https://media.licdn.com/dms/image/v2/D4E03A")
        assert not _is_noise("Bengaluru, Karnataka, India")

    def test_real_locations_still_map(self) -> None:
        texts = json.loads(FIXTURE.read_text())["payload"]["card_texts"]
        by_title = {e.title: e.location for e in map_experience_from_rsc(texts)}
        assert by_title["Software Engineer III"] == "Bengaluru, Karnataka, India"
        assert by_title["Educator"] == "India"


class TestSectionScoping:
    """Card mappers must scope to the card holding their section.

    Pooled across every card, a year-only job range ("2016 - 2022") is
    indistinguishable from a degree's date range, so education picks up board
    seats. Caught live on a profile whose board roles are year-only.
    """

    def test_texts_for_section_picks_the_right_card(self) -> None:
        from app.linkedin.strategies.flagship_web import texts_for_section

        payload = {
            "component_texts": {
                "cardA": ["Experience", "Board Member", "Acme", "2016 – 2022"],
                "cardB": ["Education", "Some University", "BSc, Physics", "2010 – 2014"],
            },
            "card_texts": ["everything", "pooled"],
        }
        got = texts_for_section(payload, "education")
        assert "Some University" in got
        assert "Board Member" not in got

    def test_falls_back_to_pooled_when_header_absent(self) -> None:
        from app.linkedin.strategies.flagship_web import texts_for_section

        payload = {"component_texts": {"cardA": ["nothing"]}, "card_texts": ["pooled"]}
        assert texts_for_section(payload, "education") == ["pooled"]

    def test_section_header_is_not_read_as_an_entry(self) -> None:
        """The header sits directly above the first entry of its card."""
        texts = ["Education", "The University of Chicago", "1994 – 1996"]
        got = map_education_from_rsc(texts)
        assert len(got) == 1
        assert got[0].school == "The University of Chicago"
        assert got[0].degree is None

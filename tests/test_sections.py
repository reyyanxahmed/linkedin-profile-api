"""Tests for section mappers against synthetic Voyager-shaped payloads.

These exercise the baseline field paths encoded from public RE knowledge. They are
NOT fixture tests — they use synthetic payloads that match the documented normalized-
envelope shape. When a real fixture arrives, add tests that read from tests/fixtures/
and verify the same outputs (fixture wins on divergence).
"""

from __future__ import annotations

from app.models import Profile
from app.normalize.sections.certifications import map_certifications
from app.normalize.sections.core import map_profile
from app.normalize.sections.education import map_education
from app.normalize.sections.experience import map_experience
from app.normalize.sections.extras import map_courses, map_honors, map_projects, map_volunteer
from app.normalize.sections.languages import map_languages
from app.normalize.sections.skills import map_skills
from app.normalize.urn_graph import UrnGraph

# --- synthetic payloads ------------------------------------------------------

RICH_PROFILE = {
    "data": {
        "*profile": "urn:li:fsd_profile:ACoAA123",
        "firstName": "Ada",
        "lastName": "Lovelace",
        "headline": "Analyst at Acme",
        "summary": "Pioneer of computing.",
        "industry": "Computer Science",
        "location": {"raw": "London, UK", "city": "London", "country": "United Kingdom", "countryCode": "GB"},
        "premiumInfo": {"premiumType": "premium"},
        "influencer": False,
        "pictureInfo": {"*rootUrl": "https://media.licdn.com/", "artifacts": [{"fileIdentifyingUrlPathSegment": "img.jpg", "width": 800, "height": 800}]},
    },
    "included": [
        {"entityUrn": "urn:li:fsd_profile:ACoAA123", "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
         "firstName": "Ada", "lastName": "Lovelace"},
        {"entityUrn": "urn:li:fs_position:(ACoAA123,1)", "$type": "com.linkedin.voyager.identity.profile.Position",
         "title": "Analyst", "employmentStatus": "Full-time",
         "*company": "urn:li:fs_miniCompany:1",
         "locationName": "London", "locationType": "On-site",
         "timePeriod": {"startDate": {"year": 2024, "month": 1}, "endDate": {}},
         "current": True, "description": "Did analysis.",
         "*skills": [{"name": "Python"}, {"name": "SQL"}]},
        {"entityUrn": "urn:li:fs_miniCompany:1", "$type": "com.linkedin.voyager.identity.profile.MiniCompany",
         "name": "Acme", "url": "https://www.linkedin.com/company/acme"},
        {"entityUrn": "urn:li:fs_education:(ACoAA123,1)", "$type": "com.linkedin.voyager.identity.profile.Education",
         "schoolName": "Cambridge", "degreeName": "BA", "fieldOfStudy": "Maths",
         "timePeriod": {"startDate": {"year": 1830}, "endDate": {"year": 1834}}},
        {"entityUrn": "urn:li:fs_skill:(ACoAA123,1)", "$type": "com.linkedin.voyager.identity.profile.Skill",
         "name": "Analytical Engine", "endorsementCount": 12},
        {"entityUrn": "urn:li:fs_certification:(ACoAA123,1)", "$type": "com.linkedin.voyager.identity.profile.Certification",
         "name": "Certified Analyst", "*authority": "urn:li:fs_organization:2",
         "timePeriod": {"startDate": {"year": 1840}}},
        {"entityUrn": "urn:li:fs_language:(ACoAA123,1)", "$type": "com.linkedin.voyager.identity.profile.Language",
         "name": "English", "proficiency": "Native or bilingual"},
    ],
}


# --- core --------------------------------------------------------------------

class TestCore:
    def test_name_and_headline(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        p = map_profile(g)
        assert p.first_name == "Ada"
        assert p.last_name == "Lovelace"
        assert p.full_name == "Ada Lovelace"
        assert p.headline == "Analyst at Acme"

    def test_about_and_industry(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        p = map_profile(g)
        assert p.about == "Pioneer of computing."
        assert p.industry == "Computer Science"

    def test_location(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        p = map_profile(g)
        assert p.location is not None
        assert p.location.raw == "London, UK"
        assert p.location.country_code == "GB"

    def test_flags_premium(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        p = map_profile(g)
        assert p.flags.premium is True

    def test_image_extracted(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        p = map_profile(g)
        assert len(p.images.profile) == 1
        assert p.images.profile[0].url.endswith("img.jpg")


# --- experience --------------------------------------------------------------

class TestExperience:
    def test_experience_one_position(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        exp = map_experience(g)
        assert len(exp) == 1
        e = exp[0]
        assert e.title == "Analyst"
        assert e.employment_type == "Full-time"
        assert e.company.name == "Acme"
        assert e.is_current is True
        assert e.start.year == 2024

    def test_experience_skills_list(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        exp = map_experience(g)
        assert exp[0].skills == ["Python", "SQL"]

    def test_experience_duration_months(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        exp = map_experience(g)
        assert exp[0].duration_months is not None
        assert exp[0].duration_months >= 0


# --- education ---------------------------------------------------------------

class TestEducation:
    def test_education_one(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        edu = map_education(g)
        assert len(edu) == 1
        assert edu[0].school == "Cambridge"
        assert edu[0].degree == "BA"
        assert edu[0].field_of_study == "Maths"
        assert edu[0].start.year == 1830
        assert edu[0].end.year == 1834


# --- skills ------------------------------------------------------------------

class TestSkills:
    def test_skills_one(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        sk = map_skills(g)
        assert len(sk) == 1
        assert sk[0].name == "Analytical Engine"
        assert sk[0].endorsement_count == 12


# --- certifications -----------------------------------------------------------

class TestCertifications:
    def test_cert_one(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        certs = map_certifications(g)
        assert len(certs) == 1
        assert certs[0].name == "Certified Analyst"
        assert certs[0].issued is not None
        assert certs[0].issued.year == 1840


# --- languages ---------------------------------------------------------------

class TestLanguages:
    def test_lang_one(self) -> None:
        g = UrnGraph(RICH_PROFILE)
        langs = map_languages(g)
        assert len(langs) == 1
        assert langs[0].name == "English"
        assert langs[0].proficiency == "Native or bilingual"


# --- empty payload robustness ------------------------------------------------

class TestEmptyPayloads:
    """Every mapper must return [] or a Profile with Nones on an empty payload.

    This is the per-mapper failure-isolation contract (Gate 3): a bad section never
    500s the whole response.
    """

    def test_empty_payload_profile(self) -> None:
        g = UrnGraph({})
        p = map_profile(g)
        assert isinstance(p, Profile)
        assert p.full_name is None

    def test_empty_payload_experience(self) -> None:
        g = UrnGraph({})
        assert map_experience(g) == []

    def test_empty_payload_education(self) -> None:
        g = UrnGraph({})
        assert map_education(g) == []

    def test_empty_payload_skills(self) -> None:
        g = UrnGraph({})
        assert map_skills(g) == []

    def test_empty_payload_certs(self) -> None:
        g = UrnGraph({})
        assert map_certifications(g) == []

    def test_empty_payload_languages(self) -> None:
        g = UrnGraph({})
        assert map_languages(g) == []

    def test_empty_payload_projects(self) -> None:
        g = UrnGraph({})
        assert map_projects(g) == []

    def test_empty_payload_honors(self) -> None:
        g = UrnGraph({})
        assert map_honors(g) == []

    def test_empty_payload_volunteer(self) -> None:
        g = UrnGraph({})
        assert map_volunteer(g) == []

    def test_empty_payload_courses(self) -> None:
        g = UrnGraph({})
        assert map_courses(g) == []


class TestMalformedPayloads:
    """Mappers must not raise on malformed entities — skip them and return the rest."""

    def test_experience_with_one_bad_entity_skips_it(self) -> None:
        payload = {
            "data": {"elements": [
                {"title": "Good", "timePeriod": {"startDate": {"year": 2020}}},
                "not a dict",
                {"no_title": True},
            ]},
            "included": [],
        }
        g = UrnGraph(payload)
        exp = map_experience(g)
        # At least the good one is mapped; the bad ones are skipped.
        assert any(e.title == "Good" for e in exp)
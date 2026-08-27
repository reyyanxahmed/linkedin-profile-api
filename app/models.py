"""Public response schema (Pydantic v2).

Single responsibility: define the typed response models for the API. These double as
OpenAPI documentation at /docs. Serialise with exclude_none=False so the shape is
stable across responses (collections are always arrays, never null).

See BUILD_SPEC.md section 7 for the full schema and design rules.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CacheMeta(BaseModel):
    hit: bool = False
    age_seconds: int = 0
    stale: bool = False


class Meta(BaseModel):
    profile_url: str
    public_identifier: str
    profile_urn: str | None = None
    fetched_at: str
    source: str
    supplemented_by: list[str] = Field(default_factory=list)
    cache: CacheMeta = Field(default_factory=CacheMeta)
    partial_sections: list[str] = Field(default_factory=list)
    completeness: float = 0.0
    request_id: str


class Location(BaseModel):
    raw: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    country_code: str | None = None


class ProfileFlags(BaseModel):
    premium: bool = False
    influencer: bool = False
    open_to_work: bool = False
    hiring: bool = False


class Image(BaseModel):
    url: str
    width: int | None = None
    height: int | None = None
    expires_at: str | None = None


class ProfileImages(BaseModel):
    profile: list[Image] = Field(default_factory=list)
    background: list[Image] = Field(default_factory=list)


class Counts(BaseModel):
    followers: int | None = None
    connections: int | None = None


class Profile(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    headline: str | None = None
    about: str | None = None
    location: Location | None = None
    industry: str | None = None
    pronouns: str | None = None
    flags: ProfileFlags = Field(default_factory=ProfileFlags)
    images: ProfileImages = Field(default_factory=ProfileImages)
    counts: Counts = Field(default_factory=Counts)


class CompanyRef(BaseModel):
    name: str | None = None
    urn: str | None = None
    linkedin_url: str | None = None
    logo: str | None = None


class DatePart(BaseModel):
    year: int | None = None
    month: int | None = None
    day: int | None = None
    iso: str | None = None


class Experience(BaseModel):
    title: str | None = None
    employment_type: str | None = None
    company: CompanyRef | None = None
    location: str | None = None
    location_type: str | None = None
    start: DatePart | None = None
    end: DatePart | None = None
    is_current: bool = False
    duration_months: int | None = None
    description: str | None = None
    skills: list[str] = Field(default_factory=list)


class Education(BaseModel):
    school: str | None = None
    school_urn: str | None = None
    school_logo: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    grade: str | None = None
    start: DatePart | None = None
    end: DatePart | None = None
    activities: str | None = None
    description: str | None = None


class Skill(BaseModel):
    name: str
    endorsement_count: int = 0


class Certification(BaseModel):
    name: str | None = None
    authority: str | None = None
    license_number: str | None = None
    url: str | None = None
    issued: DatePart | None = None
    expires: DatePart | None = None


class Language(BaseModel):
    name: str | None = None
    proficiency: str | None = None


class Project(BaseModel):
    title: str | None = None
    description: str | None = None
    url: str | None = None
    start: DatePart | None = None
    end: DatePart | None = None


class Honor(BaseModel):
    title: str | None = None
    issuer: str | None = None
    description: str | None = None
    issued: DatePart | None = None


class Volunteer(BaseModel):
    title: str | None = None
    organization: str | None = None
    description: str | None = None
    start: DatePart | None = None
    end: DatePart | None = None


class Course(BaseModel):
    name: str | None = None
    number: str | None = None
    description: str | None = None


class ProfileResponse(BaseModel):
    """The full API response. Stable shape; collections always arrays."""

    model_config = ConfigDict(use_enum_values=True)

    meta: Meta
    profile: Profile
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    publications: list[dict] = Field(default_factory=list)
    honors: list[Honor] = Field(default_factory=list)
    volunteer: list[Volunteer] = Field(default_factory=list)
    courses: list[Course] = Field(default_factory=list)


CORE_FIELDS = ("full_name", "headline", "about", "location", "experience", "education", "skills")


def compute_completeness(profile: Profile, experience: list, education: list, skills: list) -> float:
    """Populated core fields / expected core fields, rounded to 2 places.

    Core fields: full_name, headline, location, about, experience, education, skills.
    """
    populated = 0
    total = len(CORE_FIELDS)
    if profile.full_name:
        populated += 1
    if profile.headline:
        populated += 1
    if profile.about:
        populated += 1
    if profile.location and (profile.location.raw or profile.location.city):
        populated += 1
    if experience:
        populated += 1
    if education:
        populated += 1
    if skills:
        populated += 1
    return round(populated / total, 2) if total else 0.0
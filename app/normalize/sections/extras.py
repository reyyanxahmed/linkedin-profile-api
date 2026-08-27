"""Extras section mappers: projects, publications, honors, volunteer, courses.

Baseline field paths for the lower-priority sections. Each is small and follows the
same shape: find the typed entities in the payload, map to the model. Cut from the
bottom of BUILD_SPEC.md section 9 if time runs short.
"""

from __future__ import annotations

from typing import Any

from app.models import Course, DatePart, Honor, Project, Volunteer
from app.normalize.dates import parse_date
from app.normalize.urn_graph import UrnGraph


def _find_entities(graph: UrnGraph, root: Any, keys: tuple[str, ...], type_suffixes: tuple[str, ...]) -> list[dict]:
    """Find entities by star-key path first, then by_type as a fallback.

    Entities found via by_type are resolved through the graph so star-key refs
    (e.g. *school, *organization) are inlined.
    """
    out: list[dict] = []
    if isinstance(root, dict):
        for key in keys:
            nodes = root.get(key)
            if isinstance(nodes, list):
                out.extend(n for n in nodes if isinstance(n, dict))
                if out:
                    return out
    if isinstance(graph, UrnGraph):
        for suffix in type_suffixes:
            raw = graph.by_type(suffix)
            if raw:
                return [graph.resolve(e) for e in raw]
    return out


def map_projects(graph: UrnGraph, raw: dict | None = None) -> list[Project]:
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})
    ents = _find_entities(graph, root, ("projectView", "projects", "elements"), (".Project", "Project"))
    return [_map_project(e) for e in ents]


def map_publications(graph: UrnGraph, raw: dict | None = None) -> list[dict]:
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})
    ents = _find_entities(graph, root, ("publicationView", "publications", "elements"), (".Publication", "Publication"))
    # Publications are cut-without-regret; return raw dicts to keep it simple.
    return [
        {
            "title": e.get("name") or e.get("title"),
            "description": e.get("description"),
            "url": e.get("url"),
            "date": parse_date(e.get("date") or {}),
        }
        for e in ents
    ]


def map_honors(graph: UrnGraph, raw: dict | None = None) -> list[Honor]:
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})
    ents = _find_entities(graph, root, ("honorView", "honors", "elements"), (".Honor", "Honor"))
    return [_map_honor(e) for e in ents]


def map_volunteer(graph: UrnGraph, raw: dict | None = None) -> list[Volunteer]:
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})
    ents = _find_entities(graph, root, ("volunteerExperienceView", "volunteer", "elements"), (".VolunteerExperience", "Volunteer"))
    return [_map_volunteer(e) for e in ents]


def map_courses(graph: UrnGraph, raw: dict | None = None) -> list[Course]:
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})
    ents = _find_entities(graph, root, ("courseView", "courses", "elements"), (".Course", "Course"))
    return [_map_course(e) for e in ents]


def _map_project(e: dict) -> Project:
    tp = e.get("timePeriod") or {}
    start = parse_date(tp.get("startDate") or {})
    end = parse_date(tp.get("endDate") or {})
    return Project(
        title=e.get("title") or e.get("name"),
        description=e.get("description"),
        url=e.get("url"),
        start=DatePart(**start) if start else None,
        end=DatePart(**end) if end else None,
    )


def _map_honor(e: dict) -> Honor:
    issued = parse_date(e.get("issueDate") or e.get("timePeriod", {}).get("startDate") or {})
    return Honor(
        title=e.get("title") or e.get("name"),
        issuer=e.get("issuer") or e.get("issuerName"),
        description=e.get("description"),
        issued=DatePart(**issued) if issued else None,
    )


def _map_volunteer(e: dict) -> Volunteer:
    tp = e.get("timePeriod") or {}
    start = parse_date(tp.get("startDate") or {})
    end = parse_date(tp.get("endDate") or {})
    org = e.get("organization") or e.get("*organization")
    org_name = None
    if isinstance(org, dict):
        org_name = org.get("name")
    elif isinstance(org, str):
        org_name = org
    return Volunteer(
        title=e.get("title") or e.get("role"),
        organization=org_name,
        description=e.get("description") or e.get("cause"),
        start=DatePart(**start) if start else None,
        end=DatePart(**end) if end else None,
    )


def _map_course(e: dict) -> Course:
    return Course(
        name=e.get("name") or e.get("courseName") or e.get("title"),
        number=e.get("number") or e.get("courseNumber"),
        description=e.get("description"),
    )
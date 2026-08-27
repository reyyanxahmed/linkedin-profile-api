"""Education section mapper.

Baseline field paths: Education entity ($type endswith .Education) nests under
data.*educationView or data.*elements. School resolves via *school -> MiniSchool.
"""

from __future__ import annotations

from typing import Any

from app.models import DatePart, Education
from app.normalize.dates import parse_date
from app.normalize.urn_graph import UrnGraph


def map_education(graph: UrnGraph, raw: dict | None = None) -> list[Education]:
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})

    schools: list[dict] = []
    for key in ("educationView", "educations", "elements"):
        nodes = root.get(key) if isinstance(root, dict) else None
        if isinstance(nodes, list):
            for n in nodes:
                if isinstance(n, dict):
                    if "schoolName" in n or "school" in n or "degreeName" in n:
                        schools.append(n)
            if schools:
                break

    if not schools and isinstance(graph, UrnGraph):
        raw = graph.by_type(".Education") or graph.by_type("Education")
        schools = [graph.resolve(s) for s in raw]

    return [_map_one(s) for s in schools]


def _map_one(s: dict) -> Education:
    school_obj = s.get("school") or s.get("*school")
    school_name = s.get("schoolName")
    school_urn = None
    school_logo = None
    if isinstance(school_obj, dict):
        school_name = school_name or school_obj.get("name") or school_obj.get("schoolName")
        school_urn = school_obj.get("entityUrn") or school_obj.get("urn")
        school_logo = _extract_logo(school_obj.get("logo") or school_obj.get("*logo"))
    elif isinstance(school_obj, str):
        school_urn = school_obj

    degree = s.get("degreeName") or s.get("degree")
    field = s.get("fieldOfStudy") or s.get("field")
    grade = s.get("grade")
    activities = s.get("activities") or s.get("activitiesAndSocieties")
    description = s.get("description") or s.get("notes")

    tp = s.get("timePeriod") or {}
    start = parse_date(tp.get("startDate") or {})
    end = parse_date(tp.get("endDate") or {})

    return Education(
        school=school_name,
        school_urn=school_urn,
        school_logo=school_logo,
        degree=degree,
        field_of_study=field,
        grade=grade,
        start=DatePart(**start) if start else None,
        end=DatePart(**end) if end else None,
        activities=activities,
        description=description,
    )


def _extract_logo(logo: Any) -> str | None:
    if isinstance(logo, str):
        return logo
    if isinstance(logo, dict):
        root = logo.get("rootUrl") or logo.get("*rootUrl") or ""
        artifacts = logo.get("artifacts") or logo.get("*artifacts") or []
        if isinstance(artifacts, list) and artifacts:
            first = artifacts[0]
            if isinstance(first, dict):
                seg = first.get("fileIdentifyingUrlPathSegment")
                if isinstance(seg, str):
                    return root + seg if not seg.startswith("http") else seg
        return logo.get("url")
    return None
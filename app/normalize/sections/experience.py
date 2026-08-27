"""Experience section mapper.

Baseline field paths encoded from public Voyager RE knowledge. The dash/GraphQL
Position entity ($type endswith .Position or PositionGroup) nests under
data.*positionGroup or data.*elements, with timePeriod -> startDate/endDate ->
{year, month, day}. Companies resolve via *company -> MiniCompany/FsdCompany.
"""

from __future__ import annotations

from typing import Any

from app.models import CompanyRef, DatePart, Experience
from app.normalize.dates import duration_months, parse_date
from app.normalize.urn_graph import UrnGraph


def map_experience(graph: UrnGraph, raw: dict | None = None) -> list[Experience]:
    """Map experience positions from a resolved Voyager payload."""
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})

    # Candidate collections of position entities, in priority order.
    positions: list[dict] = []

    # Graph path: data.*positionGroup -> [{*positionView}] -> positions, or
    # data.*elements directly if they are positions.
    for key in ("positionGroup", "positions", "elements"):
        nodes = root.get(key) if isinstance(root, dict) else None
        if isinstance(nodes, list):
            for n in nodes:
                if isinstance(n, dict):
                    # positionGroup contains *positionView list; unwrap.
                    pv = n.get("positionView") or n.get("*positionView")
                    if isinstance(pv, list):
                        positions.extend(p for p in pv if isinstance(p, dict))
                    elif "$type" in n or "title" in n:
                        positions.append(n)
            if positions:
                break

    # Fallback: scan included for Position entities, then resolve each through the
    # graph so star-key refs (e.g. *company) are inlined.
    if not positions and isinstance(graph, UrnGraph):
        raw_positions = graph.by_type(".Position") or graph.by_type("Position")
        positions = [graph.resolve(p) for p in raw_positions]

    return [_map_one(p) for p in positions]


def _map_one(p: dict) -> Experience:
    title = p.get("title") or p.get("name")
    company = _map_company(p.get("company") or p.get("*company"))
    emp_type = p.get("employmentStatus") or p.get("employmentType")
    location = p.get("locationName") or p.get("location")
    location_type = p.get("locationType") or p.get("workLocationType")

    tp = p.get("timePeriod") or {}
    start_raw = tp.get("startDate") or {}
    end_raw = tp.get("endDate") or {}
    start = parse_date(start_raw)
    end = parse_date(end_raw)
    is_current = bool(p.get("current") or not end_raw)

    dur = duration_months(start, end, is_current=is_current)
    description = p.get("description")
    skills_raw = p.get("skills") or p.get("*skills") or []
    skills = [s.get("name") if isinstance(s, dict) else str(s) for s in skills_raw if s]

    return Experience(
        title=title,
        employment_type=emp_type,
        company=company,
        location=location,
        location_type=location_type,
        start=DatePart(**start) if start else None,
        end=DatePart(**end) if end else None,
        is_current=is_current,
        duration_months=dur,
        description=description,
        skills=skills,
    )


def _map_company(company: Any) -> CompanyRef | None:
    if not company:
        return None
    if isinstance(company, str):
        # Raw URN; we lost the name (company not in included). Best effort.
        return CompanyRef(urn=company, name=None)
    if isinstance(company, dict):
        return CompanyRef(
            name=company.get("name"),
            urn=company.get("entityUrn") or company.get("urn"),
            linkedin_url=company.get("url") or company.get("linkedinUrl"),
            logo=_extract_logo(company.get("logo") or company.get("*logo")),
        )
    return None


def _extract_logo(logo: Any) -> str | None:
    if logo is None:
        return None
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
        return logo.get("url") or logo.get("displayImageReference")
    return None
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


def _as_text(v: object) -> str | None:
    """Coerce a Voyager value that may be a string or an entity dict into text.

    Voyager is inconsistent about this: `locationName` is a plain string on legacy
    Position entities, but the dash generation hands back a ProfileLocation entity
    ({'countryCode': 'US', ...}) under `location`. Feeding that dict straight into a
    `str | None` model field raises, and one raising mapper empties the whole
    experience section — so normalize the shape here.
    """
    if v is None or (isinstance(v, (list, dict)) and not v):
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        for key in ("locationName", "defaultLocalizedName", "name", "text", "countryCode"):
            got = v.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
        return None
    return str(v)


def _location_text(p: dict) -> str | None:
    """Best available location string for a position, across all three generations."""
    for key in ("locationName", "geoLocationName", "location", "region"):
        text = _as_text(p.get(key))
        if text:
            return text
    return None


def _map_one(p: dict) -> Experience:
    title = p.get("title") or p.get("name")
    company = _map_company(p.get("company") or p.get("*company"), p)
    emp_type = p.get("employmentStatus") or p.get("employmentType")
    location = _location_text(p)
    location_type = _as_text(p.get("locationType") or p.get("workLocationType"))

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


def _map_company(company: Any, position: dict | None = None) -> CompanyRef | None:
    """Build a CompanyRef from a position's company reference.

    The legacy Position entity keeps the company NAME on the position itself
    (`companyName`) while `company` holds only a PositionCompany with industry and
    headcount — no name at all. Reading the name from the sub-entity alone yields
    null companies on every legacy position, so the position is passed in as the
    authoritative name source and the sub-entity fills in the rest.
    """
    position = position or {}
    name_on_position = position.get("companyName")
    urn_on_position = position.get("companyUrn")

    if isinstance(company, str):
        # Raw URN; the company entity was not in `included`. Best effort.
        return CompanyRef(urn=company, name=name_on_position)
    if isinstance(company, dict):
        return CompanyRef(
            name=company.get("name") or name_on_position,
            urn=company.get("entityUrn") or company.get("urn") or urn_on_position,
            linkedin_url=company.get("url") or company.get("linkedinUrl"),
            logo=_extract_logo(company.get("logo") or company.get("*logo")),
        )
    if name_on_position or urn_on_position:
        return CompanyRef(name=name_on_position, urn=urn_on_position)
    return None
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
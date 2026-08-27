"""Certifications section mapper.

Baseline: Certification entity ($type endswith .Certification) under
data.*certificationView or data.*elements. Fields: name, authority, licenseNumber,
url, timePeriod.startDate (issued), timePeriod.endDate (expires).
"""

from __future__ import annotations

from app.models import Certification, DatePart
from app.normalize.dates import parse_date
from app.normalize.urn_graph import UrnGraph


def map_certifications(graph: UrnGraph, raw: dict | None = None) -> list[Certification]:
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})

    certs: list[dict] = []
    for key in ("certificationView", "certifications", "elements"):
        nodes = root.get(key) if isinstance(root, dict) else None
        if isinstance(nodes, list):
            for n in nodes:
                if isinstance(n, dict):
                    if "name" in n or "certification" in n:
                        certs.append(n)
            if certs:
                break

    if not certs and isinstance(graph, UrnGraph):
        raw = graph.by_type(".Certification") or graph.by_type("Certification")
        certs = [graph.resolve(c) for c in raw]

    return [_map_one(c) for c in certs]


def _map_one(c: dict) -> Certification:
    name = c.get("name") or c.get("certificationName")
    authority_obj = c.get("authority") or c.get("*authority") or c.get("certificationAuthority")
    authority = None
    if isinstance(authority_obj, dict):
        authority = authority_obj.get("name") or authority_obj.get("localizedName")
    elif isinstance(authority_obj, str):
        authority = authority_obj

    license_num = c.get("licenseNumber")
    url = c.get("url") or c.get("certificationUrl")

    tp_start = c.get("timePeriod") or c.get("issueDate") or {}
    issued = parse_date(tp_start.get("startDate") or tp_start) if tp_start else None
    expires = parse_date(c.get("expiryDate") or {})
    return Certification(
        name=name,
        authority=authority,
        license_number=license_num,
        url=url,
        issued=DatePart(**issued) if issued else None,
        expires=DatePart(**expires) if expires else None,
    )
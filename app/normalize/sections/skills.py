"""Skills section mapper.

Baseline: Skill entity ($type endswith .Skill) under data.*skillView or
data.*elements. Fields: name, endorsementCount/numEndorsements.
"""

from __future__ import annotations

from app.models import Skill
from app.normalize.urn_graph import UrnGraph


def map_skills(graph: UrnGraph, raw: dict | None = None) -> list[Skill]:
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})

    skills: list[dict] = []
    for key in ("skillView", "skills", "elements"):
        nodes = root.get(key) if isinstance(root, dict) else None
        if isinstance(nodes, list):
            for n in nodes:
                if isinstance(n, dict):
                    if "name" in n or "skill" in n:
                        skills.append(n)
            if skills:
                break

    if not skills and isinstance(graph, UrnGraph):
        raw = graph.by_type(".Skill") or graph.by_type("Skill")
        skills = [graph.resolve(s) for s in raw]

    return [_map_one(s) for s in skills]


def _map_one(s: dict) -> Skill:
    # Skill entities nest the actual name under 'skill' ref or 'name' directly.
    name = s.get("name")
    if not name:
        inner = s.get("skill") or s.get("*skill")
        if isinstance(inner, dict):
            name = inner.get("name")
        elif isinstance(inner, str):
            name = inner
    endorsements = s.get("endorsementCount") or s.get("numEndorsements") or 0
    try:
        count = int(endorsements)
    except (TypeError, ValueError):
        count = 0
    return Skill(name=name or "(unknown)", endorsement_count=count)
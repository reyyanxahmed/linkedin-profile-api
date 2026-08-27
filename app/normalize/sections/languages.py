"""Languages section mapper.

Baseline: Language entity ($type endswith .Language) under data.*languageView or
data.*elements. Fields: name, proficiency.
"""

from __future__ import annotations

from app.models import Language
from app.normalize.urn_graph import UrnGraph


def map_languages(graph: UrnGraph, raw: dict | None = None) -> list[Language]:
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})

    langs: list[dict] = []
    for key in ("languageView", "languages", "elements"):
        nodes = root.get(key) if isinstance(root, dict) else None
        if isinstance(nodes, list):
            for n in nodes:
                if isinstance(n, dict):
                    if "name" in n or "language" in n or "proficiency" in n:
                        langs.append(n)
            if langs:
                break

    if not langs and isinstance(graph, UrnGraph):
        raw = graph.by_type(".Language") or graph.by_type("Language")
        langs = [graph.resolve(lang) for lang in raw]

    return [_map_one(lang) for lang in langs]


def _map_one(lang: dict) -> Language:
    name = lang.get("name")
    if not name:
        inner = lang.get("language") or lang.get("*language")
        if isinstance(inner, dict):
            name = inner.get("name")
        elif isinstance(inner, str):
            name = inner
    proficiency = lang.get("proficiency")
    if isinstance(proficiency, dict):
        proficiency = proficiency.get("localizedName") or proficiency.get("name")
    return Language(name=name, proficiency=proficiency)
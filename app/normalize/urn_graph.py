"""URN graph resolver — the intellectual core of the submission.

Single responsibility: take a LinkedIn "normalized envelope" payload (the shape returned
with `accept: application/vnd.linkedin.normalized+json+2.1`) and resolve its URN
reference graph into inlined Python objects.

PURE. No I/O, no async, no config, no logging, no imports from app.linkedin. This is
what makes it testable offline against synthetic fixtures, which is what lets the whole
project iterate at 2am when every session is rate limited.

Envelope shape (see BUILD_SPEC.md 4.3):
    {
      "data": { "*elements": ["urn:..."], "*profile": "urn:...", ... },
      "included": [ { "entityUrn": "urn:...", "$type": "...", ... }, ... ]
    }

Rules:
- Keys prefixed with '*' hold URN references: a single URN string, or a list of them.
- `included` is a flat pool of every entity referenced anywhere in the response.
- Resolve by indexing `included` by `entityUrn` and inlining.

Guards (both mandatory, see BUILD_SPEC.md 6.2):
- MAX_DEPTH = 12. Beyond it, return the raw URN string instead of recursing.
- `seen` is the set of URNs on the current resolution path. If a URN is already in
  `seen`, return the raw URN string. This is what makes cycles terminate.

Design note: return the raw URN string on a cycle or depth-cap hit rather than None.
It preserves information and makes debugging tractable.
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 12


class UrnGraph:
    """Index a normalized-envelope payload and resolve its URN references.

    Construct once per payload. Call .root() for the fully resolved data, or
    .get(urn) / .by_type(suffix) for targeted lookups.
    """

    def __init__(self, payload: dict) -> None:
        """Index payload['included'] by 'entityUrn'. Store payload['data'].

        Tolerates payloads missing 'included' or 'data' (returns empty graph).
        """
        self._data: Any = payload.get("data", {}) if isinstance(payload, dict) else {}
        included = payload.get("included", []) if isinstance(payload, dict) else []
        # Index by entityUrn. Some entities lack entityUrn (rare); keep them out of the
        # index but they remain in _included_list for by_type() scans.
        self._index: dict[str, dict] = {}
        self._included_list: list[dict] = []
        if isinstance(included, list):
            for ent in included:
                if not isinstance(ent, dict):
                    continue
                self._included_list.append(ent)
                urn = ent.get("entityUrn")
                if isinstance(urn, str):
                    self._index[urn] = ent

    def get(self, urn: str) -> dict | None:
        """Look up one entity by URN. None if absent."""
        return self._index.get(urn)

    def resolve(
        self,
        node: Any,
        *,
        depth: int = 0,
        seen: frozenset[str] = frozenset(),
    ) -> Any:
        """Recursively inline URN references.

        - dict: star-keys (*foo) are replaced by their resolved entity; non-star keys
          are recursed into. Returns a new dict (never mutates input).
        - list: each element resolved.
        - scalar: returned unchanged.
        - A URN string under a star-key that is not in `included` becomes None
          (single) or is dropped (list).
        """
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k.startswith("*"):
                    bare = k[1:]
                    out[bare] = self._resolve_ref(v, depth=depth, seen=seen)
                else:
                    out[k] = self.resolve(v, depth=depth + 1, seen=seen)
            return out
        if isinstance(node, list):
            return [self.resolve(el, depth=depth, seen=seen) for el in node]
        return node

    def _resolve_ref(self, ref: Any, *, depth: int, seen: frozenset[str]) -> Any:
        """Resolve a star-key value: a single URN or a list of URNs.

        The depth cap applies here, at the point of following a URN reference.
        Beyond MAX_DEPTH, return the raw URN string instead of recursing further.
        """
        if depth >= MAX_DEPTH:
            return ref
        if isinstance(ref, list):
            resolved: list[Any] = []
            for urn in ref:
                if not isinstance(urn, str):
                    # Non-string element in a ref list; pass through resolved.
                    resolved.append(self.resolve(urn, depth=depth, seen=seen))
                    continue
                if urn in seen:
                    # Cycle: preserve the raw URN string.
                    resolved.append(urn)
                    continue
                ent = self.get(urn)
                if ent is None:
                    # Not in included: drop, per spec ("dropping the ones not found").
                    continue
                resolved.append(self.resolve(ent, depth=depth + 1, seen=seen | {urn}))
            return resolved
        if isinstance(ref, str):
            if ref in seen:
                return ref
            ent = self.get(ref)
            if ent is None:
                return None
            return self.resolve(ent, depth=depth + 1, seen=seen | {ref})
        # Non-string, non-list ref (rare). Resolve in place.
        return self.resolve(ref, depth=depth, seen=seen)

    def root(self) -> Any:
        """Fully resolved payload['data']."""
        return self.resolve(self._data)

    def by_type(self, type_suffix: str) -> list[dict]:
        """Every entity in `included` whose `$type` endswith type_suffix.

        The escape hatch: when the `data` graph does not lead where you need, pull all
        entities of a type straight out of `included`. Section mappers should prefer
        graph traversal and fall back to this, noting why in a comment.
        """
        out: list[dict] = []
        for ent in self._included_list:
            t = ent.get("$type")
            if isinstance(t, str) and t.endswith(type_suffix):
                out.append(ent)
        return out
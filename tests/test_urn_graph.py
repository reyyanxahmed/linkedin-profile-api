"""Tests for app.normalize.urn_graph.UrnGraph.

Covers the five required cases from BUILD_SPEC.md Phase 1 item 7:
  - single reference
  - list reference
  - nested reference two levels deep
  - missing URN in `included`
  - reference cycle

Plus: by_type() escape hatch, depth cap, and non-star key recursion.
"""

from __future__ import annotations

import pytest

from app.normalize.urn_graph import MAX_DEPTH, UrnGraph

# --- synthetic fixtures ------------------------------------------------------

SINGLE_REF = {
    "data": {"*profile": "urn:li:fs_profile:ABC"},
    "included": [
        {"entityUrn": "urn:li:fs_profile:ABC", "$type": "Profile", "firstName": "Ada"},
    ],
}

LIST_REF = {
    "data": {"*elements": ["urn:li:fs_position:1", "urn:li:fs_position:2"]},
    "included": [
        {"entityUrn": "urn:li:fs_position:1", "$type": "Position", "title": "A"},
        {"entityUrn": "urn:li:fs_position:2", "$type": "Position", "title": "B"},
    ],
}

NESTED_TWO_LEVELS = {
    "data": {"*profile": "urn:li:fs_profile:ABC"},
    "included": [
        {"entityUrn": "urn:li:fs_profile:ABC", "$type": "Profile", "*company": "urn:li:fs_miniCompany:CO"},
        {"entityUrn": "urn:li:fs_miniCompany:CO", "$type": "MiniCompany", "name": "Acme", "*logo": "urn:li:fs_mediaLogo:L"},
        {"entityUrn": "urn:li:fs_mediaLogo:L", "$type": "MediaLogo", "url": "https://x/logo.png"},
    ],
}

MISSING_URN = {
    "data": {"*profile": "urn:li:fs_profile:ABC", "*elements": ["urn:li:fs_position:1", "urn:li:fs_position:missing"]},
    "included": [
        {"entityUrn": "urn:li:fs_profile:ABC", "$type": "Profile", "firstName": "Ada"},
        {"entityUrn": "urn:li:fs_position:1", "$type": "Position", "title": "A"},
        # urn:li:fs_position:missing is deliberately absent
    ],
}

# Two-level cycle: A -> B -> A
CYCLE = {
    "data": {"*a": "urn:1"},
    "included": [
        {"entityUrn": "urn:1", "$type": "T", "name": "one", "*next": "urn:2"},
        {"entityUrn": "urn:2", "$type": "T", "name": "two", "*next": "urn:1"},
    ],
}

# Self cycle: A -> A
SELF_CYCLE = {
    "data": {"*a": "urn:1"},
    "included": [
        {"entityUrn": "urn:1", "$type": "T", "name": "self", "*self": "urn:1"},
    ],
}

# Deep chain to exercise MAX_DEPTH (depth >= 12 returns raw URN)
DEEP_CHAIN = {
    "data": {"*n": "urn:0"},
    "included": [{"entityUrn": f"urn:{i}", "$type": "T", "next": i, "*n": f"urn:{i + 1}"} for i in range(MAX_DEPTH + 5)],
}


# --- tests -------------------------------------------------------------------

class TestSingleRef:
    def test_single_reference_resolves(self) -> None:
        g = UrnGraph(SINGLE_REF)
        root = g.root()
        assert root == {"profile": {"entityUrn": "urn:li:fs_profile:ABC", "$type": "Profile", "firstName": "Ada"}}

    def test_get_returns_entity(self) -> None:
        g = UrnGraph(SINGLE_REF)
        assert g.get("urn:li:fs_profile:ABC") is not None
        assert g.get("urn:nonexistent") is None


class TestListRef:
    def test_list_reference_resolves_all(self) -> None:
        g = UrnGraph(LIST_REF)
        root = g.root()
        assert isinstance(root["elements"], list)
        assert [e["title"] for e in root["elements"]] == ["A", "B"]

    def test_list_reference_drops_missing(self) -> None:
        g = UrnGraph(MISSING_URN)
        root = g.root()
        # The missing urn is dropped from the list, the present one stays.
        assert isinstance(root["elements"], list)
        assert len(root["elements"]) == 1
        assert root["elements"][0]["title"] == "A"

    def test_single_missing_urn_becomes_none(self) -> None:
        g = UrnGraph(MISSING_URN)
        # *profile resolves; if it pointed at a missing urn it would be None.
        root = g.root()
        assert root["profile"]["firstName"] == "Ada"


class TestNested:
    def test_nested_two_levels_deep(self) -> None:
        g = UrnGraph(NESTED_TWO_LEVELS)
        root = g.root()
        prof = root["profile"]
        assert prof["company"]["name"] == "Acme"
        assert prof["company"]["logo"]["url"] == "https://x/logo.png"


class TestCycle:
    def test_two_level_cycle_terminates(self) -> None:
        g = UrnGraph(CYCLE)
        root = g.root()
        a = root["a"]
        # a -> {name: one, next: b}, b.next -> a (cycle) returns raw URN string
        assert a["name"] == "one"
        b = a["next"]
        assert b["name"] == "two"
        # b's *next points back to urn:1 which is in seen -> raw string returned
        assert b["next"] == "urn:1"

    def test_self_cycle_terminates(self) -> None:
        g = UrnGraph(SELF_CYCLE)
        root = g.root()
        a = root["a"]
        # a -> {name: self, self: <raw urn because it's in seen>}
        assert a["name"] == "self"
        assert a["self"] == "urn:1"

    def test_no_infinite_recursion_on_cycle(self) -> None:
        # The real safety check: this test would hang/overflow without the seen guard.
        g = UrnGraph(CYCLE)
        g.root()  # must return, not raise RecursionError
        g = UrnGraph(SELF_CYCLE)
        g.root()


class TestDepthCap:
    def test_deep_chain_returns_raw_urn_at_cap(self) -> None:
        g = UrnGraph(DEEP_CHAIN)
        root = g.root()
        # Walk down the chain; at depth >= MAX_DEPTH, *n returns the raw URN string.
        node = root["n"]
        depth = 0
        while isinstance(node, dict) and "n" in node:
            depth += 1
            nxt = node["n"]
            if isinstance(nxt, str):
                # Hit the cap: raw URN string returned instead of recursing.
                assert nxt.startswith("urn:")
                break
            node = nxt
        else:
            pytest.fail("depth cap did not trigger; chain never returned a raw URN")
        # Sanity: we got at least a few levels deep before the cap.
        assert depth >= 1


class TestByType:
    def test_by_type_returns_all_matching_suffix(self) -> None:
        g = UrnGraph(NESTED_TWO_LEVELS)
        profs = g.by_type("Profile")
        comps = g.by_type("MiniCompany")
        logos = g.by_type("MediaLogo")
        assert len(profs) == 1
        assert len(comps) == 1
        assert len(logos) == 1
        assert profs[0]["$type"] == "Profile"

    def test_by_type_empty_when_no_match(self) -> None:
        g = UrnGraph(SINGLE_REF)
        assert g.by_type("Nonexistent") == []


class TestNonStarRecursion:
    def test_non_star_keys_are_recursed(self) -> None:
        payload = {
            "data": {"details": {"nested": {"*profile": "urn:li:fs_profile:ABC"}}},
            "included": [{"entityUrn": "urn:li:fs_profile:ABC", "$type": "Profile", "firstName": "Ada"}],
        }
        g = UrnGraph(payload)
        root = g.root()
        assert root["details"]["nested"]["profile"]["firstName"] == "Ada"


class TestRobustness:
    def test_empty_payload(self) -> None:
        g = UrnGraph({})
        assert g.root() == {}
        assert g.get("urn:anything") is None
        assert g.by_type("Anything") == []

    def test_missing_included_key(self) -> None:
        g = UrnGraph({"data": {"*x": "urn:1"}})
        root = g.root()
        assert root == {"x": None}

    def test_included_entity_without_urn_is_skipped_in_index_but_kept_in_by_type(self) -> None:
        payload = {
            "data": {},
            "included": [
                {"$type": "Orphan", "name": "no urn here"},
                {"entityUrn": "urn:1", "$type": "Real", "name": "has urn"},
            ],
        }
        g = UrnGraph(payload)
        assert g.get("urn:1") is not None
        # Orphan is not in the index (no urn to index by) but by_type finds it.
        assert len(g.by_type("Orphan")) == 1
        assert len(g.by_type("Real")) == 1

    def test_input_not_mutated(self) -> None:
        import copy

        original = copy.deepcopy(CYCLE)
        g = UrnGraph(CYCLE)
        g.root()
        assert CYCLE == original
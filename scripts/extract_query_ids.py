"""Extract GraphQL queryId and dash decorationId values from a browser HAR file.

Single responsibility: walk a HAR's entries, find Voyager GraphQL and dash-profile
requests, collect their queryId and decorationId query parameters, and print them
grouped in a form that can be pasted straight into app/linkedin/queries.yaml.

Usage:
    python scripts/extract_query_ids.py <capture.har>

The values rotate with LinkedIn frontend deploys, so this is the recipe to refresh them
when the GraphQL strategy breaks. See BUILD_SPEC.md section 11 (what the human must
supply) and README "Known limitations".
"""

from __future__ import annotations

import json
import sys
import urllib.parse as up
from pathlib import Path


def extract(har_path: Path) -> dict[str, set[str]]:
    """Return {'queryId': {...}, 'decorationId': {...}, 'graphql_paths': set()}.

    queryId values come from /voyager/api/graphql URLs.
    decorationId values come from /identity/dash/profiles URLs.
    """
    har = json.loads(har_path.read_text())
    query_ids: set[str] = set()
    decoration_ids: set[str] = set()
    graphql_endpoints: set[str] = set()

    entries = har.get("log", {}).get("entries", [])
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        if not url:
            continue

        # Split URL into path and query. urllib handles the query string.
        parsed = up.urlsplit(url)
        path = parsed.path or ""
        query = parsed.query

        if "/voyager/api/graphql" in path or "/graphql" in path:
            graphql_endpoints.add(path)
            params = up.parse_qs(query)
            for key in ("queryId", "operationName"):
                if key in params:
                    for v in params[key]:
                        query_ids.add(v)

        if "/identity/dash/profiles" in path:
            params = up.parse_qs(query)
            if "decorationId" in params:
                for v in params["decorationId"]:
                    decoration_ids.add(v)

    return {
        "queryId": query_ids,
        "decorationId": decoration_ids,
        "graphql_paths": graphql_endpoints,
    }


def _format_yaml_block(name: str, values: set[str]) -> str:
    if not values:
        return f"{name}:  # none found in this HAR\n  # paste real value(s) here\n"
    lines = [f"{name}:"]
    for v in sorted(values):
        lines.append(f"  - {v}")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/extract_query_ids.py <capture.har>", file=sys.stderr)
        return 2

    har_path = Path(argv[1])
    if not har_path.exists():
        print(f"error: file not found: {har_path}", file=sys.stderr)
        return 2

    result = extract(har_path)

    print("=" * 72)
    print("Paste the following into app/linkedin/queries.yaml:")
    print("=" * 72)
    print()
    print("# GraphQL queryId values (rotate with LinkedIn frontend deploys).")
    print("# Refresh with: python scripts/extract_query_ids.py <fresh.har>")
    print(_format_yaml_block("graphql_query_ids", result["queryId"]))
    print()
    print("# Dash decorationId values (rotate with deploys).")
    print(_format_yaml_block("dash_decoration_ids", result["decorationId"]))
    print()
    if result["graphql_paths"]:
        print("# GraphQL endpoint paths seen:")
        for p in sorted(result["graphql_paths"]):
            print(f"#   {p}")
        print()

    if not result["queryId"] and not result["decorationId"]:
        print("WARNING: no queryId or decorationId found. Did you capture Voyager traffic?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
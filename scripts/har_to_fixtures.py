"""Convert a browser HAR capture into redacted offline test fixtures.

Single responsibility: for each HAR entry whose URL hits /voyager/api/, write the
response body as pretty JSON to tests/fixtures/<slugified-endpoint>-<n>.json, REDACTED.

Redaction before writing:
  - strip Cookie and Set-Cookie request/response headers
  - walk the JSON and replace values for keys matching
    email|emailAddress|phoneNumber|address|birthDate with "[REDACTED]"

Prints a summary of what was redacted, a warning that fixtures contain real third-party
profile data (the human must review before committing), and the list of written files.

Usage:
    python scripts/har_to_fixtures.py <capture.har> tests/fixtures/
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse as up
from pathlib import Path

# Keys whose values must be redacted. Match on the final path component (the key),
# case-insensitive. Keep the list conservative — over-redaction hides bugs.
REDACT_KEYS = re.compile(r"^(email|emailAddress|phoneNumber|address|birthDate|phoneNumber.*)$", re.I)

# Header names to strip entirely from the saved fixture (never persist credentials).
STRIP_HEADERS = re.compile(r"^(cookie|set-cookie|authorization|csrf-token|x-li-token|li-at|jsessionid)$", re.I)


def slugify(s: str) -> str:
    # Turn a URL path into a safe filename component.
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "endpoint"


def redact_headers(headers: list[list[str]] | list[dict] | None) -> list[list[str]]:
    """Drop credential-bearing headers. Accept HAR's [[k,v],...] or [{k,v},...] shapes."""
    out: list[list[str]] = []
    if not headers:
        return out
    for h in headers:
        if isinstance(h, dict):
            k = str(h.get("name", ""))
            v = str(h.get("value", ""))
        elif isinstance(h, (list, tuple)) and len(h) == 2:
            k, v = str(h[0]), str(h[1])
        else:
            continue
        if STRIP_HEADERS.match(k):
            out.append([k, "[REDACTED]"])
        else:
            out.append([k, v])
    return out


def redact_body(obj, counter: list[int]) -> object:
    """Recursively redact sensitive values. Returns a new structure; never mutates input."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and REDACT_KEYS.match(k):
                counter[0] += 1
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_body(v, counter)
        return out
    if isinstance(obj, list):
        return [redact_body(el, counter) for el in obj]
    return obj


def convert(har_path: Path, out_dir: Path) -> tuple[list[Path], int]:
    """Convert HAR to fixtures. Returns (written_files, redaction_count)."""
    har = json.loads(har_path.read_text())
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = har.get("log", {}).get("entries", [])

    written: list[Path] = []
    redaction_count = 0
    n = 0
    for entry in entries:
        req = entry.get("request", {})
        res = entry.get("response", {})
        url = req.get("url", "")
        if "/voyager/api/" not in url:
            continue

        parsed = up.urlsplit(url)
        path = parsed.path or "endpoint"
        slug = slugify(path)
        filename = f"{slug}-{n}.json"
        n += 1

        body = res.get("content", {}).get("text", "")
        try:
            body_obj = json.loads(body) if body else None
        except json.JSONDecodeError:
            body_obj = {"_raw": body, "_note": "body was not valid JSON"}

        counter = [0]
        redacted_body = redact_body(body_obj, counter)
        redaction_count += counter[0]

        fixture = {
            "_url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "_query": parsed.query,
            "_request_headers": redact_headers(req.get("headers")),
            "_response_headers": redact_headers(res.get("headers")),
            "_status": res.get("status"),
            "body": redacted_body,
        }

        out_path = out_dir / filename
        out_path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False))
        written.append(out_path)

    return written, redaction_count


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python scripts/har_to_fixtures.py <capture.har> <out_dir>", file=sys.stderr)
        return 2

    har_path = Path(argv[1])
    out_dir = Path(argv[2])
    if not har_path.exists():
        print(f"error: HAR not found: {har_path}", file=sys.stderr)
        return 2

    written, redactions = convert(har_path, out_dir)
    print(f"Wrote {len(written)} fixture(s) to {out_dir}/")
    print(f"Redacted {redactions} sensitive value(s).")
    for p in written:
        print(f"  {p}")

    if written:
        print()
        print("WARNING: fixtures contain real third-party profile data.")
        print("Review each file before committing. Consider using profiles belonging to")
        print("consenting people or to the account holder.")
        print()
        print("Before commit, run:")
        print("  grep -ri 'li_at\\|jsessionid' --include='*.json' tests/fixtures/")
    else:
        print("No /voyager/api/ entries found in HAR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
"""Image URL handling: parse signed-CDN expiry out of LinkedIn media URLs.

Single responsibility: given a LinkedIn media CDN URL, return (url, expires_at_iso).
LinkedIn media URLs are signed and time-limited; surfacing expires_at prevents
consumers from caching a dead link. No I/O, pure.
"""

from __future__ import annotations

import urllib.parse as up


def parse_image_expiry(url: str | None) -> str | None:
    """Return the expiry timestamp from a LinkedIn media URL, or None.

    LinkedIn media URLs typically encode expiry in one of these query params:
      - 'expires_at'  (seconds since epoch, as a string)
      - 'e'           (compact form)
      - 'Expires'     (header-style, sometimes in the URL)
    We return an ISO 8601 string if we can parse it, else None.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = up.urlsplit(url)
        params = up.parse_qs(parsed.query)
    except Exception:
        return None
    for key in ("expires_at", "e", "Expires", "expires"):
        if key in params:
            val = params[key][0]
            ts = _to_iso(val)
            if ts:
                return ts
    return None


def _to_iso(val: str) -> str | None:
    """Convert a numeric-or-iso string to an ISO 8601 timestamp, or None."""
    try:
        import datetime as _dt

        # Try unix seconds.
        if val.isdigit():
            return _dt.datetime.fromtimestamp(int(val), tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Try parsing as ISO already.
        return _dt.datetime.fromisoformat(val.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None
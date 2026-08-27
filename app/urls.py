"""LinkedIn profile URL normalisation.

Single responsibility: turn any accepted LinkedIn profile URL form into a bare public
identifier (slug), or raise InvalidUrlError. No I/O, no config. Depends only on the
standard library and app.errors.

Accepts: www/non-www, http/https, country subdomains (in.linkedin.com), trailing slashes,
query/fragment, percent-encoded unicode slugs, and bare-slug passthrough.
Rejects: company/school/pub/dir/feed paths, non-linkedin hosts, empty input.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from app.errors import InvalidUrlError

# Hosts we accept. Comparison is case-insensitive against the registered host suffix.
_LINKEDIN_HOST_SUFFIXES = (
    "linkedin.com",
)

# Path prefixes that are NOT personal profiles. Reject these explicitly so the API
# returns a clear 400 rather than a confusing 404 from LinkedIn.
_REJECTED_PREFIXES = (
    "/company/",
    "/school/",
    "/pub/dir/",
    "/feed",
    "/jobs/",
    "/groups/",
    "/company",
    "/school",
    "/pub/dir",
)

# The profile path marker. Everything after it (up to /, ?, #) is the slug.
_PROFILE_MARKER = "/in/"


def normalize_profile_url(raw: str) -> str:
    """Return the bare public identifier (slug) from any LinkedIn profile URL form.

    Raises:
        InvalidUrlError: empty input, non-linkedin host, rejected path, missing slug.
    """
    if not raw or not raw.strip():
        raise InvalidUrlError("URL is empty")

    s = raw.strip()

    # Bare slug passthrough: no scheme, no host, no slash. Treat as the slug itself.
    # Must not look like a path or URL.
    if "://" not in s and "/" not in s and "." not in s.split("-")[-1] and not s.startswith("/"):
        slug = unquote(s).strip()
        if not slug:
            raise InvalidUrlError("URL is empty after decode")
        return slug

    # If no scheme, urlsplit mis-parses the host as a path. Prepend a scheme.
    if "://" not in s:
        s = "https://" + s

    try:
        parts = urlsplit(s)
    except ValueError as e:
        raise InvalidUrlError(f"unparseable URL: {e}") from e

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidUrlError("URL has no host")

    if not any(host == suf or host.endswith("." + suf) for suf in _LINKEDIN_HOST_SUFFIXES):
        raise InvalidUrlError(f"not a linkedin.com URL: {host}")

    path = parts.path or "/"

    # Reject non-profile path prefixes early with a clear code.
    for bad in _REJECTED_PREFIXES:
        if path == bad or path.startswith(bad.rstrip("/") + "/") or path == bad.rstrip("/"):
            raise InvalidUrlError(f"not a personal profile URL: {path}")

    # Find the /in/ marker. It may sit at the root or after a locale prefix (/en/in/...).
    # Search case-insensitively so /IN/ matches, but extract the slug from the original
    # (case-preserved) path so slugs like Some-Slug keep their casing.
    lower_path = path.lower()
    idx = lower_path.find(_PROFILE_MARKER)
    if idx == -1:
        # Could be a /pub/ profile (legacy). Accept /pub/<slug> as a profile form but
        # only the slug, not the full pub path with segments. /pub/dir/ is rejected above.
        if lower_path.startswith("/pub/"):
            tail = path[len("/pub/"):]
        else:
            raise InvalidUrlError(f"not a profile URL (no /in/ marker): {path}")
    else:
        tail = path[idx + len(_PROFILE_MARKER):]

    # The slug is everything up to the next slash.
    slug_raw = tail.split("/", 1)[0]
    slug = unquote(slug_raw).strip()

    if not slug:
        raise InvalidUrlError("profile slug is empty")

    return slug
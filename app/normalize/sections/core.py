"""Core profile section mapper: name, headline, about, location, images, counts.

Baseline field paths encoded from public Voyager reverse-engineering knowledge + the
BUILD_SPEC.md schema. Calibrate against a real fixture when one arrives; on divergence
the fixture wins (leave a comment).
"""

from __future__ import annotations

from typing import Any

from app.models import Counts, Image, Location, Profile, ProfileFlags, ProfileImages
from app.normalize.images import parse_image_expiry
from app.normalize.urn_graph import UrnGraph


def map_profile(graph: UrnGraph, raw: dict | None = None) -> Profile:
    """Map the core profile fields from a resolved Voyager payload.

    Reads from the graph root (data.*) and falls back to by_type for the Profile entity.
    Robust to missing fields — returns a Profile with Nones, not an error.
    """
    root = graph.root() if isinstance(graph, UrnGraph) else (raw or {})
    # Find the profile entity: either the resolved root, or a Profile-typed entity.
    prof = _find_profile_entity(graph, root)

    first = _get(prof, "firstName", "first_name") or ""
    last = _get(prof, "lastName", "last_name") or ""
    full = _get(prof, "fullName", "full_name") or (f"{first} {last}".strip() if (first or last) else None)

    headline = _get(prof, "headline")
    about = _get(prof, "summary", "about")
    industry = _get(prof, "industry")
    pronouns = _str(_get(prof, "pronouns"))

    loc = _map_location(_get(prof, "location", "defaultLocation") or {})
    flags = _map_flags(prof)
    images = _map_images(prof)
    counts = _map_counts(prof, root)

    return Profile(
        first_name=first or None,
        last_name=last or None,
        full_name=full,
        headline=headline,
        about=about,
        location=loc,
        industry=industry,
        pronouns=pronouns,
        flags=flags,
        images=images,
        counts=counts,
    )


def _find_profile_entity(graph: UrnGraph, root: Any) -> dict:
    """Locate the profile entity in the payload. Merge root-level fields with the
    resolved *profile entity (root takes precedence — it is the canonical `data`).
    """
    if isinstance(root, dict):
        # Start with the resolved *profile entity if present.
        prof: dict = {}
        inner = root.get("profile")
        if isinstance(inner, dict):
            prof = dict(inner)
        # Merge in root-level fields (data.* flattened into data by the resolver
        # when they are not star-keys). Root wins because it is the canonical data.
        for k, v in root.items():
            if k == "profile":
                continue
            if k.startswith("*"):
                continue
            if v is not None:
                prof.setdefault(k, v)
        # If the root itself looks like a profile (legacy profileView data shape),
        # use it directly.
        if not prof and ("firstName" in root or "first_name" in root or "headline" in root):
            return root
        if prof:
            return prof
    # Fallback: scan included for a Profile-typed entity.
    if isinstance(graph, UrnGraph):
        profs = graph.by_type(".Profile")
        if profs:
            return graph.resolve(profs[0])
        profs = graph.by_type("IdentityProfile")
        if profs:
            return graph.resolve(profs[0])
    return {}


def _get(d: dict, *keys: str) -> Any:
    """Return the first present value for any of the candidate keys."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    # pronounsView / objects — pull .pronouns or .value
    if isinstance(v, dict):
        return v.get("pronouns") or v.get("value")
    return str(v)


def _map_location(loc: Any) -> Location | None:
    if not loc or not isinstance(loc, dict):
        return None
    raw = loc.get("raw") or loc.get("defaultLocalizedName") or loc.get("name")
    city = loc.get("city") or loc.get("preferredGeoLocation")
    region = loc.get("region") or loc.get("standardizedLocation")
    country = loc.get("country") or loc.get("countryName")
    code = loc.get("countryCode") or loc.get("countryCode_")
    if not any([raw, city, region, country, code]):
        return None
    return Location(raw=raw, city=city, region=region, country=country, country_code=code)


def _map_flags(prof: dict) -> ProfileFlags:
    premium = bool(_get(prof, "premiumInfo"))
    influencer = bool(_get(prof, "influencer"))
    open_to_work = bool(_get(prof, "openToWork", "open_to_work"))
    hiring = bool(_get(prof, "hiring", "openToHiring"))
    return ProfileFlags(premium=premium, influencer=influencer, open_to_work=open_to_work, hiring=hiring)


def _map_images(prof: dict) -> ProfileImages:
    profile_imgs: list[Image] = []
    bg_imgs: list[Image] = []

    pic = _get(prof, "pictureInfo", "picture", "profilePicture")
    if isinstance(pic, dict):
        for url in _extract_image_urls(pic):
            profile_imgs.append(Image(url=url, expires_at=parse_image_expiry(url)))
    elif isinstance(pic, str):
        profile_imgs.append(Image(url=pic, expires_at=parse_image_expiry(pic)))

    bg = _get(prof, "backgroundImage", "backgroundPicture", "backgroundInfo")
    if isinstance(bg, dict):
        for url in _extract_image_urls(bg):
            bg_imgs.append(Image(url=url, expires_at=parse_image_expiry(url)))
    elif isinstance(bg, str):
        bg_imgs.append(Image(url=bg, expires_at=parse_image_expiry(bg)))

    return ProfileImages(profile=profile_imgs, background=bg_imgs)


def _extract_image_urls(pic: dict) -> list[str]:
    """Pull image URLs from a pictureInfo/artifacts-shaped dict."""
    urls: list[str] = []
    # Common shape: { *rootUrl: "...", artifacts: [ { fileIdentifyingUrlPathSegment, width, height } ] }
    root = pic.get("rootUrl") or pic.get("*rootUrl") or ""
    artifacts = pic.get("artifacts") or pic.get("*artifacts") or []
    if isinstance(artifacts, list):
        for a in artifacts:
            if not isinstance(a, dict):
                continue
            seg = a.get("fileIdentifyingUrlPathSegment")
            if isinstance(seg, str):
                urls.append(root + seg if not seg.startswith("http") else seg)
    # Also accept a direct 'url' or 'displayImageReference'.
    if not urls:
        url = pic.get("url") or pic.get("displayImageReference")
        if isinstance(url, str):
            urls.append(url)
    return urls


def _map_counts(prof: dict, root: Any) -> Counts:
    followers = _get(prof, "followers", "followerCount")
    connections = _get(prof, "connections", "connectionCount")
    # Sometimes counts come from a separate entity in included; try by_type if missing.
    if followers is None and isinstance(root, dict):
        for v in (root.get("followerCount"), root.get("followers")):
            if v is not None:
                followers = v
                break
    return Counts(
        followers=int(followers) if isinstance(followers, (int, str)) and str(followers).isdigit() else None,
        connections=int(connections) if isinstance(connections, (int, str)) and str(connections).isdigit() else None,
    )
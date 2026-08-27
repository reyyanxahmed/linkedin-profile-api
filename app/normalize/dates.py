"""Partial-date handling for LinkedIn profile dates.

Single responsibility: parse LinkedIn date dicts (often partial: year-only, or
year+month) into a stable {year, month, day, iso} representation WITHOUT inventing a
day when only year/month is known. Compute duration_months between two dates.

No I/O, no config. Pure functions.
"""

from __future__ import annotations

import datetime as _dt


def parse_date(raw: dict | None) -> dict | None:
    """Parse a LinkedIn date dict.

    {'year': 2025, 'month': 8} -> {'year': 2025, 'month': 8, 'day': None, 'iso': '2025-08'}
    {'year': 2025}             -> {'year': 2025, 'month': None, 'day': None, 'iso': '2025'}
    None or {}                 -> None

    `iso` is a year, or year-month, never a full date unless a day was actually present.
    """
    if not raw or not isinstance(raw, dict):
        return None
    year = raw.get("year")
    if year is None:
        # No year means no usable date.
        return None
    year = int(year)
    month = raw.get("month")
    day = raw.get("day")
    month = int(month) if month is not None else None
    day = int(day) if day is not None else None

    iso = _iso(year, month, day)
    return {"year": year, "month": month, "day": day, "iso": iso}


def _iso(year: int, month: int | None, day: int | None) -> str:
    if month is None:
        return f"{year:04d}"
    if day is None:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def duration_months(
    start: dict | None,
    end: dict | None,
    *,
    is_current: bool,
) -> int | None:
    """Month count between two parsed dates.

    - If end is None and is_current, measure to today.
    - If start has no month, assume January (LinkedIn omits month for older roles).
    - Return None if start is None.
    - Returns 0 if end < start (defensive; shouldn't happen but never negative).
    """
    if not start or not isinstance(start, dict):
        return None
    sy = start.get("year")
    if sy is None:
        return None
    sm = start.get("month") or 1  # assume January if month missing

    if is_current or not end or not isinstance(end, dict) or end.get("year") is None:
        ey_now, em_now = _today_year_month()
        ey, em = ey_now, em_now
    else:
        ey = int(end["year"])
        em = int(end.get("month") or 1)

    months = (ey - int(sy)) * 12 + (em - int(sm))
    return max(0, months)


def _today_year_month() -> tuple[int, int]:
    today = _dt.date.today()
    return today.year, today.month
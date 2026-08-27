"""Tests for app.normalize.dates.

Covers parse_date (year-only, year+month, year+month+day, empty), and
duration_months (current role, completed range, missing month, missing end).
"""

from __future__ import annotations

import datetime as _dt

import pytest

from app.normalize.dates import duration_months, parse_date


class TestParseDate:
    @pytest.mark.parametrize(
        "raw, expected_iso",
        [
            ({"year": 2025, "month": 8, "day": 15}, "2025-08-15"),
            ({"year": 2025, "month": 8}, "2025-08"),
            ({"year": 2025}, "2025"),
            (None, None),
            ({}, None),
            ({"month": 8}, None),  # year missing -> unusable
        ],
    )
    def test_parse(self, raw, expected_iso) -> None:
        out = parse_date(raw)
        if expected_iso is None:
            assert out is None
        else:
            assert out is not None
            assert out["iso"] == expected_iso

    def test_year_only_carries_none_month_day(self) -> None:
        out = parse_date({"year": 2025})
        assert out == {"year": 2025, "month": None, "day": None, "iso": "2025"}

    def test_year_month_carries_none_day(self) -> None:
        out = parse_date({"year": 2025, "month": 8})
        assert out == {"year": 2025, "month": 8, "day": None, "iso": "2025-08"}

    def test_full_date(self) -> None:
        out = parse_date({"year": 2025, "month": 8, "day": 15})
        assert out == {"year": 2025, "month": 8, "day": 15, "iso": "2025-08-15"}

    def test_never_invents_day_when_only_year_month(self) -> None:
        out = parse_date({"year": 2020, "month": 1})
        assert out is not None
        assert out["day"] is None
        assert out["iso"] == "2020-01"  # not 2020-01-01


class TestDurationMonths:
    def test_current_role_measured_to_today(self) -> None:
        start = {"year": 2020, "month": 1}
        out = duration_months(start, None, is_current=True)
        now = _dt.date.today()
        expected = (now.year - 2020) * 12 + (now.month - 1)
        assert out == expected

    def test_completed_range(self) -> None:
        start = {"year": 2020, "month": 1}
        end = {"year": 2021, "month": 7}
        assert duration_months(start, end, is_current=False) == 18

    def test_start_missing_month_assumes_january(self) -> None:
        start = {"year": 2020}  # no month -> January
        end = {"year": 2021, "month": 1}
        assert duration_months(start, end, is_current=False) == 12

    def test_end_missing_month_assumes_january(self) -> None:
        start = {"year": 2020, "month": 6}
        end = {"year": 2021}  # no month -> January
        assert duration_months(start, end, is_current=False) == 7

    def test_start_none_returns_none(self) -> None:
        assert duration_months(None, {"year": 2021, "month": 1}, is_current=False) is None
        assert duration_months({}, {"year": 2021, "month": 1}, is_current=False) is None

    def test_start_no_year_returns_none(self) -> None:
        assert duration_months({"month": 6}, {"year": 2021, "month": 1}, is_current=False) is None

    def test_current_role_with_end_still_uses_today(self) -> None:
        # is_current True overrides end.
        start = {"year": 2020, "month": 1}
        end = {"year": 2019, "month": 1}  # end < start, but is_current wins
        out = duration_months(start, end, is_current=True)
        assert out is not None
        assert out >= 0

    def test_end_before_start_returns_zero(self) -> None:
        start = {"year": 2021, "month": 6}
        end = {"year": 2021, "month": 1}
        # Defensive: never negative.
        assert duration_months(start, end, is_current=False) == 0

    def test_end_none_not_current_returns_today(self) -> None:
        # If is_current is False but end is None/missing year, fall back to today.
        start = {"year": 2020, "month": 1}
        out = duration_months(start, None, is_current=False)
        assert out is not None
        assert out >= 0
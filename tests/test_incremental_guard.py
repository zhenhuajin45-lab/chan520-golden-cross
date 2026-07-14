from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from chan520_skill.incremental_guard import DataAccessGuard, FutureDataAccessViolation


@dataclass(frozen=True)
class Row:
    code: str
    date: date


def test_data_access_guard_allows_session_date_and_before():
    guard = DataAccessGuard(date(2026, 1, 6))
    guard.assert_not_future(date(2026, 1, 6), source="daily_bars", code="600288")
    guard.assert_not_future(date(2026, 1, 5), source="daily_bars", code="600288")
    guard.assert_rows_not_future([Row("600288", date(2026, 1, 6))], source="rows")
    assert guard.violations == []


def test_data_access_guard_rejects_future_date():
    guard = DataAccessGuard(date(2026, 1, 6))
    with pytest.raises(FutureDataAccessViolation):
        guard.assert_not_future(date(2026, 1, 7), source="daily_bars", code="600288")
    assert guard.violations == [
        {
            "source": "daily_bars",
            "code": "600288",
            "date": "2026-01-07",
            "session_date": "2026-01-06",
            "error_code": "FUTURE_DATA_ACCESS_VIOLATION",
        }
    ]

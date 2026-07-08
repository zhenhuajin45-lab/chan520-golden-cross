from __future__ import annotations

from datetime import date, timedelta

import pytest

from chan520_skill.models import KLine
from chan520_skill.quality import DataQualityError, data_quality, ensure_data_quality


def make_row(day: date, close: float = 10.0) -> KLine:
    return KLine(day, close, close, close + 1, close - 1, 1000, 0, 0, 0, 0, 0)


def test_data_quality_passes_clean_rows() -> None:
    rows = [make_row(date(2024, 1, 1) + timedelta(days=i)) for i in range(61)]
    assert data_quality(rows) == []


def test_data_quality_reports_dirty_ohlc() -> None:
    rows = [make_row(date(2024, 1, 1) + timedelta(days=i)) for i in range(61)]
    rows[-1] = KLine(rows[-1].date, 10, 11, 9, 12, 1000, 0, 0, 0, 0, 0)
    issues = data_quality(rows)
    assert any("high < low" in item for item in issues)
    assert any("high < max" in item for item in issues)
    with pytest.raises(DataQualityError):
        ensure_data_quality(rows)


def test_data_quality_reports_duplicate_dates() -> None:
    rows = [make_row(date(2024, 1, 1) + timedelta(days=i)) for i in range(60)]
    rows.append(make_row(rows[-1].date))
    assert any("duplicate date" in item for item in data_quality(rows))

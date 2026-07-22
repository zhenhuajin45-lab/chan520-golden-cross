from __future__ import annotations

from datetime import date

from chan520_skill.market_store import (
    load_history,
    load_minute_day,
    load_scan,
    upsert_history,
    upsert_minute_day,
    upsert_scan,
)
from chan520_skill.models import KLine


def bar(day: int) -> KLine:
    value = float(day)
    return KLine(date(2026, 1, day), value, value, value, value, 1, 0, 0, 0, 0, 0)


def test_store_requires_an_exact_target_date_for_history(tmp_path):
    path = tmp_path / "market.db"
    rows = [bar(day) for day in range(1, 6)]
    upsert_history("600001", "示例", 1, rows, source="test", path=path)

    assert load_history("600001", date(2026, 1, 5), minimum_bars=5, path=path) is not None
    assert load_history("600001", date(2026, 1, 6), minimum_bars=5, path=path) is None


def test_store_round_trips_scan_and_same_date_minutes(tmp_path):
    path = tmp_path / "market.db"
    upsert_scan(date(2026, 7, 21), [{"code": "600001", "name": "示例"}], {"coverage_pass": True}, source="test", path=path)
    minute = {"name": "示例", "prev_close": 10.0, "open": 10.1, "minutes": {"0930": 10.1}}
    upsert_minute_day("600001", date(2026, 7, 21), minute, is_index=False, source="test", path=path)

    scan = load_scan(date(2026, 7, 21), path=path)
    cached = load_minute_day("600001", date(2026, 7, 21), is_index=False, path=path)
    assert scan is not None and scan[0][0]["code"] == "600001"
    assert cached is not None and cached["prev_close"] == 10.0
    assert load_minute_day("600001", date(2026, 7, 22), is_index=False, path=path) is None

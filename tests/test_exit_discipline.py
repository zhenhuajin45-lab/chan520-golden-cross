from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.backtest import Position, _exit_by_discipline
from chan520_skill.models import IndicatorPoint, KLine
from chan520_skill.risk import RiskConfig, trailing_stop


def make_history() -> list[KLine]:
    rows = []
    highs = [10, 11, 12, 12, 11.8, 11.7, 11.6]
    for idx, high in enumerate(highs):
        day = date(2024, 1, 1) + timedelta(days=idx)
        rows.append(KLine(day, high - 0.5, high - 0.4, high, high - 1, 1000, 0, 1, 0, 0, 0))
    return rows


def point(day: date) -> IndicatorPoint:
    return IndicatorPoint(day, 10, 10, 9, 8, None, None, 1, 0, 1, 55, 1, 1, 1, 1)


def test_three_days_no_new_high_triggers_exit() -> None:
    rows = make_history()
    pos = Position("600000", "测试", "行业", 100, 10, rows[0].date, "entry", 9, 12, holding_bars=3)
    assert _exit_by_discipline(pos, rows[-1], rows, rows[-1].date, point(rows[-1].date), RiskConfig()) == "three_bars_no_high"


def test_trailing_stop_moves_up() -> None:
    assert trailing_stop(10, 10.2, 12, 1, RiskConfig(), current_stop=9) == 9
    assert trailing_stop(10, 15, 12, 1, RiskConfig(), current_stop=9) == 13


def test_time_stop_exit_path() -> None:
    rows = make_history()
    late_day = rows[0].date + timedelta(days=8)
    row = KLine(late_day, 10.1, 10.1, 10.2, 9.9, 1000, 0, 1, 0, 0, 0)
    pos = Position("600000", "测试", "行业", 100, 10, rows[0].date, "entry", 9, 10.2, holding_bars=7)
    assert _exit_by_discipline(pos, row, rows + [row], row.date, point(row.date), RiskConfig()) == "time_stop"

from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.backtest import Position, _add_signal
from chan520_skill.models import IndicatorPoint, KLine


def row(idx: int, close: float, high: float | None = None, low: float | None = None, volume: float = 1000) -> KLine:
    day = date(2024, 1, 1) + timedelta(days=idx)
    return KLine(day, close, close, high or close + 0.2, low or close - 0.2, volume, 0, 1, 1, 0, 0)


def point(day: date, ma: float = 10.5, volume: float = 1.2) -> IndicatorPoint:
    return IndicatorPoint(day, ma, ma, ma, 9, None, None, 1, 0, 1, 55, volume, 1, 1, volume)


def test_profitable_breakout_can_add() -> None:
    rows = [row(idx, 10 + idx * 0.05) for idx in range(25)]
    breakout = row(25, 12.0, high=12.5, low=11.6, volume=2000)
    rows.append(breakout)
    pos = Position("600000", "测试", "行业", 1000, 10, rows[0].date, "entry", 9, 11.8)
    assert _add_signal(pos, breakout, rows, breakout.date, point(breakout.date, ma=11.5))


def test_no_add_when_losing_or_below_stop() -> None:
    rows = [row(idx, 10 + idx * 0.02) for idx in range(25)]
    weak = row(25, 9.8, high=10.0, low=9.0)
    rows.append(weak)
    pos = Position("600000", "测试", "行业", 1000, 10, rows[0].date, "entry", 9.9, 10.5)
    assert not _add_signal(pos, weak, rows, weak.date, point(weak.date, ma=10.2))

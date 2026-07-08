from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.models import KLine
from chan520_skill.regime import degrade_verdict, evaluate_regime


def make_rows(start_price: float, step: float, days: int = 90) -> list[KLine]:
    rows = []
    day = date(2024, 1, 1)
    price = start_price
    for idx in range(days):
        price += step
        rows.append(KLine(day + timedelta(days=idx), price, price, price + 1, price - 1, 1000, 0, 0, 0, 0, 0))
    return rows


def test_regime_trend_up() -> None:
    rows = make_rows(100, 1.0)
    state = evaluate_regime("000001", rows, rows[-1].date)
    assert state.regime == "trend_up"
    assert state.regime_ok


def test_regime_down() -> None:
    rows = make_rows(200, -1.0)
    state = evaluate_regime("000001", rows, rows[-1].date)
    assert state.regime == "down"
    assert not state.regime_ok


def test_regime_range_and_verdict_degrade() -> None:
    rows = make_rows(100, 0.01)
    state = evaluate_regime("000001", rows, rows[-1].date)
    assert state.regime == "range"
    assert degrade_verdict("入选") == "观察"
    assert degrade_verdict("观察（轻仓试探）") == "回避/减仓观察"

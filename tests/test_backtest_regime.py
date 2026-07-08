from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.backtest import _regime_by_date_from_index
from chan520_skill.models import KLine


def make_index(start_price: float, step: float) -> list[KLine]:
    rows = []
    price = start_price
    for idx in range(90):
        day = date(2024, 1, 1) + timedelta(days=idx)
        price += step
        rows.append(KLine(day, price, price, price + 1, price - 1, 100000, 0, 1, 1, 0, 0))
    return rows


def test_index_regime_replaces_basket_width() -> None:
    days = [date(2024, 3, 15), date(2024, 3, 20)]
    up = _regime_by_date_from_index("000300", make_index(100, 0.5), days)
    down = _regime_by_date_from_index("000300", make_index(100, -0.5), days)
    assert all(state.regime_ok for state in up.values())
    assert all(not state.regime_ok for state in down.values())

from __future__ import annotations

from datetime import date

from chan520_skill.microstructure import is_limit_down, is_limit_up, is_new_stock, price_limit
from chan520_skill.models import KLine


def bar(close: float, high: float | None = None, low: float | None = None) -> KLine:
    return KLine(date(2024, 1, 2), close, close, high or close, low or close, 1000, 0, 0, 0, 0, 0)


def test_price_limit_by_board() -> None:
    assert price_limit("600000") == 10
    assert price_limit("000001") == 10
    assert price_limit("300001") == 20
    assert price_limit("688001") == 20
    assert price_limit("830000") == 30
    assert price_limit("600000", is_st=True) == 5


def test_limit_up_and_down_with_tolerance() -> None:
    assert is_limit_up(bar(10.98), 10.0, "600000")
    assert not is_limit_up(bar(10.5, high=10.8), 10.0, "600000")
    assert is_limit_down(bar(9.02), 10.0, "600000")


def test_new_stock_threshold() -> None:
    rows = [bar(10.0) for _ in range(249)]
    assert is_new_stock(rows, min_history=250)
    assert not is_new_stock(rows + [bar(10.0)], min_history=250)

from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.models import KLine, RegimeState, StockMeta
from chan520_skill.strategy import analyze


def make_rows(days: int = 80) -> list[KLine]:
    rows = []
    price = 10.0
    for idx in range(days):
        day = date(2024, 1, 1) + timedelta(days=idx)
        price += 0.03
        rows.append(KLine(day, price, price, price + 0.1, price - 0.1, 10000 + idx, 0, 0, 0, 0, 0))
    return rows


def test_new_stock_defect_is_explicit() -> None:
    rows = make_rows(80)
    report = analyze(StockMeta("600000", "测试", 1), rows, rows[-1].date, min_history=250)
    assert any("历史数据不足" in item for item in report.defects)


def test_regime_downgrades_observe_verdict() -> None:
    rows = make_rows(80)
    regime = RegimeState("000001", "上证指数", rows[-1].date, "down", False, "down market")
    report = analyze(StockMeta("600000", "测试", 1), rows, rows[-1].date, regime_state=regime)
    assert any("市场regime过滤未通过" in item for item in report.defects)

from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.models import KLine
from chan520_skill.sector import degrade_for_sector, industry_of, sector_state_from_members


def rows(up: bool = True) -> list[KLine]:
    out = []
    price = 10.0
    for idx in range(80):
        price += 0.1 if up else -0.05
        day = date(2024, 1, 1) + timedelta(days=idx)
        out.append(KLine(day, price, price, price + 0.1, price - 0.1, 1000, 0, 0, 0, 0, 0))
    return out


def test_industry_mapping_default() -> None:
    assert industry_of("600288") == "软件服务"
    assert industry_of("999999") == "未知行业"


def test_sector_state_breadth() -> None:
    state = sector_state_from_members("测试行业", {"a": rows(True), "b": rows(True), "c": rows(False)}, rows(True)[-1].date)
    assert state.sector_ok
    assert state.breadth >= 0.55


def test_sector_degrade() -> None:
    assert degrade_for_sector("入选") == "观察"
    assert degrade_for_sector("观察") == "回避/减仓观察"

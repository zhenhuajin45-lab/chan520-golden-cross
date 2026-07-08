from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .indicators import build_indicators, pct_change
from .models import KLine


DEFAULT_SECTOR_MAP = {
    "600288": "软件服务",
    "300568": "电池材料",
    "688106": "工业气体",
    "002132": "金属制品",
    "603638": "工程机械",
    "301282": "电子元件",
    "600203": "消费电子",
    "300390": "新能源",
    "002830": "装修装饰",
    "601199": "公用事业",
    "688584": "半导体",
    "688130": "半导体",
    "300966": "医药制造",
    "603893": "半导体",
    "002077": "半导体",
}


@dataclass(frozen=True)
class SectorState:
    industry: str
    date: date
    regime: str
    sector_ok: bool
    breadth: float
    detail: str


def industry_of(code: str, mapping: dict[str, str] | None = None) -> str:
    mapping = mapping or DEFAULT_SECTOR_MAP
    return mapping.get(code, "未知行业")


def sector_state_from_members(industry: str, member_rows: dict[str, list[KLine]], target: date) -> SectorState:
    valid: list[tuple[list[KLine], object]] = []
    for rows in member_rows.values():
        trimmed = [row for row in rows if row.date <= target]
        if len(trimmed) < 61:
            continue
        point = build_indicators(trimmed)[-1]
        valid.append((trimmed, point))
    if not valid:
        return SectorState(industry, target, "unknown", False, 0.0, "行业数据不足")
    above20 = sum(1 for rows, point in valid if point.ma20 is not None and rows[-1].close > point.ma20)
    breadth = above20 / len(valid)
    synthetic_close = sum(rows[-1].close for rows, _point in valid) / len(valid)
    synthetic_60 = sum(rows[-61].close for rows, _point in valid) / len(valid)
    rise60 = pct_change(synthetic_60, synthetic_close)
    sector_ok = breadth >= 0.55 and rise60 >= 0
    regime = "trend" if sector_ok else "weak"
    detail = f"{industry} breadth={breadth:.2%}, rise60={rise60:.2f}%, members={len(valid)}"
    return SectorState(industry, target, regime, sector_ok, breadth, detail)


def degrade_for_sector(verdict: str) -> str:
    if verdict == "入选":
        return "观察"
    if verdict.startswith("观察"):
        return "回避/减仓观察"
    return verdict

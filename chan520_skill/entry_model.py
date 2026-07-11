from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import IndicatorPoint, KLine


@dataclass(frozen=True)
class EntryScore:
    date: date
    score: int
    reasons: tuple[str, ...]


def trend_pullback_entry(rows: list[KLine], idx: int, point: IndicatorPoint) -> EntryScore:
    row = rows[idx]
    if idx < 1 or point.ma20 is None or point.ma60 is None:
        return EntryScore(row.date, 0, ("insufficient_entry_data",))
    prev = rows[idx - 1]
    score = 0
    reasons: list[str] = []
    if point.ma20 > point.ma60:
        score += 20
        reasons.append("trend_ma20_gt_ma60")
    distance = abs(row.close / point.ma20 - 1)
    if distance <= 0.04:
        score += 20
        reasons.append("near_ma20")
    if row.low <= point.ma20 * 1.03 and row.close >= point.ma20:
        score += 20
        reasons.append("pullback_reclaimed_ma20")
    if row.close > prev.close:
        score += 20
        reasons.append("price_recovered")
    if (point.volume_ratio or 0.0) >= 0.8:
        score += 10
        reasons.append("volume_confirmed")
    if point.rsi14 is not None and 40 <= point.rsi14 <= 70:
        score += 10
        reasons.append("rsi_entry_zone")
    return EntryScore(row.date, min(score, 100), tuple(reasons))

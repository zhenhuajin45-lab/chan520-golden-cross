from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .indicators import pct_change
from .models import IndicatorPoint, KLine


@dataclass(frozen=True)
class AlphaScore:
    date: date
    total: int
    trend: int
    relative_strength: int
    volume_quality: int
    risk: int
    reasons: tuple[str, ...]


def score_alpha(
    rows: list[KLine],
    idx: int,
    point: IndicatorPoint,
    index_rows_by_date: dict[date, KLine],
    index_prior_by_date: dict[tuple[date, int], KLine],
) -> AlphaScore:
    row = rows[idx]
    trend, trend_reasons = _trend(rows, idx, point)
    rs, rs_reasons = _relative_strength(rows, idx, row.date, index_rows_by_date, index_prior_by_date)
    volume, volume_reasons = _volume_quality(rows, idx, point)
    risk, risk_reasons = _risk(row, point)
    total = max(0, min(100, trend + rs + volume + risk))
    return AlphaScore(row.date, total, trend, rs, volume, risk, tuple(trend_reasons + rs_reasons + volume_reasons + risk_reasons))


def _trend(rows: list[KLine], idx: int, point: IndicatorPoint) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    row = rows[idx]
    if point.ma20 is not None and point.ma60 is not None and point.ma20 > point.ma60:
        score += 15
        reasons.append("MA20>MA60")
    if point.ma60 is not None and point.ma120 is not None and point.ma60 > point.ma120:
        score += 10
        reasons.append("MA60>MA120")
    if point.ma20 is not None and row.close > point.ma20:
        score += 10
        reasons.append("close>MA20")
    if idx >= 20 and point.ma20 is not None:
        sustained = 0
        for pos in range(idx, max(idx - 20, -1), -1):
            if pos < 19:
                continue
            historical_ma20 = sum(item.close for item in rows[pos - 19 : pos + 1]) / 20
            if rows[pos].close > historical_ma20:
                sustained += 1
        if sustained >= 12:
            score += 5
            reasons.append("trend_persistence")
    return min(score, 40), reasons


def _relative_strength(
    rows: list[KLine],
    idx: int,
    day: date,
    index_rows_by_date: dict[date, KLine],
    index_prior_by_date: dict[tuple[date, int], KLine],
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    current_index = index_rows_by_date.get(day)
    for window, points in ((20, 10), (60, 10)):
        if idx < window or current_index is None:
            continue
        prior_index = index_prior_by_date.get((day, window))
        if prior_index is None:
            continue
        stock_ret = pct_change(rows[idx - window].close, rows[idx].close)
        index_ret = pct_change(prior_index.close, current_index.close)
        if stock_ret > index_ret:
            score += points
            reasons.append(f"rs{window}={stock_ret - index_ret:.2f}")
    return score, reasons


def _volume_quality(rows: list[KLine], idx: int, point: IndicatorPoint) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    row = rows[idx]
    prev = rows[idx - 1] if idx else row
    ratio = point.volume_ratio or 0.0
    if row.close > prev.close and ratio >= 1.0:
        score += 10
        reasons.append("up_volume_confirm")
    if row.close >= prev.close and ratio <= 1.8:
        score += 5
        reasons.append("volume_not_exhausted")
    if idx >= 5:
        pullback = rows[idx - 5 : idx]
        down_sessions = sum(1 for pos in range(1, len(pullback)) if pullback[pos].close < pullback[pos - 1].close)
        if pullback and down_sessions >= 2 and row.close < max(item.close for item in pullback):
            avg_pullback_volume = sum(item.volume for item in pullback) / len(pullback)
            if row.volume <= avg_pullback_volume:
                score += 5
                reasons.append("pullback_volume_control")
    return min(score, 20), reasons


def _risk(row: KLine, point: IndicatorPoint) -> tuple[int, list[str]]:
    score = 20
    reasons: list[str] = []
    if point.atr14 is None or row.close <= 0:
        return 0, ["atr_missing"]
    atr_pct = point.atr14 / row.close
    if atr_pct > 0.06:
        score -= 15
        reasons.append("high_atr")
    elif atr_pct > 0.04:
        score -= 8
        reasons.append("mid_atr")
    if point.ma20 is not None and row.close > point.ma20 * 1.15:
        score -= 8
        reasons.append("extended_from_ma20")
    return max(0, score), reasons

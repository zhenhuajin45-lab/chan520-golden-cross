from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import mean, pstdev

from .indicators import build_indicators, pct_change
from .models import KLine, RegimeState


@dataclass(frozen=True)
class MarketRegimePoint:
    date: date
    state: str
    position_multiplier: float
    index_trend_score: int
    breadth: float
    volatility: float
    detail: str


def build_market_regime(
    index_rows: list[KLine],
    breadth_by_date: dict[date, float],
    all_dates: list[date],
) -> dict[date, MarketRegimePoint]:
    sorted_rows = sorted(index_rows, key=lambda item: item.date)
    points = build_indicators(sorted_rows)
    out: dict[date, MarketRegimePoint] = {}
    row_idx = 0
    for day in all_dates:
        while row_idx + 1 < len(sorted_rows) and sorted_rows[row_idx + 1].date <= day:
            row_idx += 1
        if not sorted_rows or sorted_rows[row_idx].date > day:
            out[day] = _point(day, "BEAR", 0.0, 0, breadth_by_date.get(day, 0.0), 0.0, "index unavailable")
            continue
        row = sorted_rows[row_idx]
        point = points[row_idx]
        breadth = breadth_by_date.get(day, 0.0)
        volatility = _realized_vol(sorted_rows, row_idx, 20)
        trend_score = 0
        if point.ma60 is not None and row.close > point.ma60:
            trend_score += 1
        if point.ma120 is not None and point.ma60 is not None and point.ma60 > point.ma120:
            trend_score += 1
        if row_idx >= 60 and pct_change(sorted_rows[row_idx - 60].close, row.close) > 0:
            trend_score += 1

        if trend_score >= 2 and breadth >= 0.55 and volatility <= 0.025:
            state, multiplier = "BULL", 1.0
        elif trend_score == 0 or breadth < 0.35 or volatility >= 0.04:
            state, multiplier = "BEAR", 0.25
        else:
            state, multiplier = "NORMAL", 0.60
        detail = f"trend_score={trend_score}; breadth={breadth:.2%}; vol20={volatility:.2%}"
        out[day] = _point(day, state, multiplier, trend_score, breadth, volatility, detail)
    return out


def to_regime_states(symbol: str, regimes: dict[date, MarketRegimePoint]) -> dict[date, RegimeState]:
    return {
        day: RegimeState(symbol, symbol, day, point.state, point.state != "BEAR", point.detail)
        for day, point in regimes.items()
    }


def _point(
    day: date,
    state: str,
    multiplier: float,
    trend_score: int,
    breadth: float,
    volatility: float,
    detail: str,
) -> MarketRegimePoint:
    return MarketRegimePoint(day, state, multiplier, trend_score, breadth, volatility, detail)


def _realized_vol(rows: list[KLine], idx: int, window: int) -> float:
    if idx < window:
        return 0.0
    returns = [
        rows[pos].close / rows[pos - 1].close - 1
        for pos in range(idx + 1 - window, idx + 1)
        if rows[pos - 1].close
    ]
    return pstdev(returns) if len(returns) > 1 else 0.0


def breadth_above_ma60(
    histories: dict[str, list[KLine]],
    points_by_date: dict[str, dict[date, object]],
    rows_by_date: dict[str, dict[date, KLine]],
    eligible_by_date: dict[date, set[str]],
    all_dates: list[date],
) -> dict[date, float]:
    out: dict[date, float] = {}
    for day in all_dates:
        eligible = eligible_by_date.get(day) or set(histories)
        valid = []
        for code in eligible:
            row = rows_by_date.get(code, {}).get(day)
            point = points_by_date.get(code, {}).get(day)
            ma60 = getattr(point, "ma60", None)
            if row is not None and ma60 is not None:
                valid.append(row.close > ma60)
        out[day] = sum(valid) / len(valid) if valid else 0.0
    return out

"""Modular trend-following and pullback strategy for research backtests.

All decisions are computed from bars up to and including the signal date.
The module deliberately returns a decision object instead of placing orders;
the portfolio engine remains responsible for D+1 execution and risk limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .indicators import build_indicators
from .models import IndicatorPoint, KLine, RegimeState


STRATEGY_ID = "strategy_v2_modular"
ENTRY_THRESHOLD = 65


@dataclass(frozen=True)
class ModularDecision:
    strategy_id: str
    date: date
    market_state: str
    trend_score: int
    entry_score: int
    momentum_score: int
    volume_score: int
    market_score: int
    risk_penalty: int
    total_score: int
    verdict: str
    reasons: tuple[str, ...]


def evaluate_modular(
    rows: list[KLine],
    target_date: date,
    regime_state: RegimeState | None = None,
    threshold: int = ENTRY_THRESHOLD,
    point: IndicatorPoint | None = None,
    previous: IndicatorPoint | None = None,
    prior_ma60: float | None = None,
) -> ModularDecision:
    if not rows or rows[-1].date != target_date:
        raise ValueError("rows must be trimmed to target_date")
    if point is None:
        points = build_indicators(rows)
        point = points[-1]
        previous = points[-2] if len(points) > 1 else None
    row = rows[-1]
    market_state = _market_state(regime_state)

    if prior_ma60 is None and len(rows) >= 65:
        prior_points = build_indicators(rows[:-5])
        prior_ma60 = prior_points[-1].ma60 if prior_points else None
    trend_score, trend_reasons = _trend_score(row, point, prior_ma60)
    entry_score, entry_reasons = _entry_score(rows, point, previous)
    momentum_score, momentum_reasons = _momentum_score(row, point, previous)
    volume_score, volume_reasons = _volume_score(point)
    market_score = {"BULL": 15, "NORMAL": 8, "BEAR": 0}.get(market_state, 0)
    risk_penalty, risk_reasons = _risk_penalty(row, point)
    total = max(0, min(100, trend_score + entry_score + momentum_score + volume_score + market_score - risk_penalty))
    eligible_market = market_state != "BEAR"
    verdict = "入选" if eligible_market and total >= threshold else "观察"
    reasons = tuple(trend_reasons + entry_reasons + momentum_reasons + volume_reasons + risk_reasons)
    return ModularDecision(
        strategy_id=STRATEGY_ID,
        date=target_date,
        market_state=market_state,
        trend_score=trend_score,
        entry_score=entry_score,
        momentum_score=momentum_score,
        volume_score=volume_score,
        market_score=market_score,
        risk_penalty=risk_penalty,
        total_score=total,
        verdict=verdict,
        reasons=reasons,
    )


def _market_state(regime_state: RegimeState | None) -> str:
    if regime_state is None:
        return "NORMAL"
    if regime_state.regime in {"BULL", "NORMAL", "BEAR"}:
        return regime_state.regime
    return {"trend_up": "BULL", "range": "NORMAL", "down": "BEAR"}.get(regime_state.regime, "NORMAL")


def _trend_score(row: KLine, point: IndicatorPoint, prior_ma60: float | None) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if point.ma20 is not None and row.close > point.ma20:
        score += 10
        reasons.append("价格站上MA20")
    if point.ma20 is not None and point.ma60 is not None and point.ma20 > point.ma60:
        score += 10
        reasons.append("MA20位于MA60上方")
    if point.ma60 is not None and point.ma120 is not None and point.ma60 > point.ma120:
        score += 10
        reasons.append("MA60位于MA120上方")
    if point.ma60 is not None and prior_ma60 is not None and point.ma60 >= prior_ma60:
        score += 10
        reasons.append("MA60保持上行")
    return score, reasons


def _entry_score(rows: list[KLine], point: IndicatorPoint, previous: IndicatorPoint | None) -> tuple[int, list[str]]:
    if point.ma20 is None or len(rows) < 2:
        return 0, []
    row = rows[-1]
    prev_row = rows[-2]
    distance = abs(row.close / point.ma20 - 1)
    touched = row.low <= point.ma20 * 1.04 and row.close >= point.ma20
    recovering = row.close > prev_row.close
    near_ma20 = distance <= 0.05
    score = 0
    reasons: list[str] = []
    if near_ma20:
        score += 10
        reasons.append("价格接近MA20")
    if touched:
        score += 10
        reasons.append("回踩MA20后收回")
    if recovering:
        score += 10
        reasons.append("回踩后动量恢复")
    return score, reasons


def _momentum_score(row: KLine, point: IndicatorPoint, previous: IndicatorPoint | None) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if point.macd_dif is not None and point.macd_dea is not None and point.macd_dif > point.macd_dea:
        score += 5
        reasons.append("MACD动能向上")
    if point.rsi14 is not None and 45 <= point.rsi14 <= 70:
        score += 5
        reasons.append("RSI处于健康区间")
    if previous is not None and point.macd_hist is not None and previous.macd_hist is not None and point.macd_hist >= previous.macd_hist:
        score += 5
        reasons.append("MACD柱体改善")
    return score, reasons


def _volume_score(point: IndicatorPoint) -> tuple[int, list[str]]:
    ratio = point.volume_ratio or 0.0
    score = 0
    reasons: list[str] = []
    if ratio >= 1.0:
        score += 5
        reasons.append("成交量不弱于近期均量")
    if ratio >= 1.2:
        score += 5
        reasons.append("量价确认")
    return score, reasons


def _risk_penalty(row: KLine, point: IndicatorPoint) -> tuple[int, list[str]]:
    penalty = 0
    reasons: list[str] = []
    if point.rsi14 is not None and point.rsi14 > 72:
        penalty += 10
        reasons.append("RSI过热")
    if point.ma20 is not None and row.close > point.ma20 * 1.12:
        penalty += 8
        reasons.append("价格偏离MA20过大")
    if point.atr14 is None:
        penalty += 10
        reasons.append("ATR不足")
    return penalty, reasons


def modular_regime_by_date(symbol: str, rows: list[KLine], all_dates: list[date]) -> dict[date, RegimeState]:
    """Build point-in-time BULL/NORMAL/BEAR states from an index series."""
    sorted_rows = sorted(rows, key=lambda item: item.date)
    points = build_indicators(sorted_rows)
    output: dict[date, RegimeState] = {}
    for day in all_dates:
        usable = [idx for idx, item in enumerate(sorted_rows) if item.date <= day]
        if not usable:
            output[day] = RegimeState(symbol, symbol, day, "BEAR", False, "index data unavailable")
            continue
        idx = usable[-1]
        row = sorted_rows[idx]
        point = points[idx]
        if point.ma120 is None or point.ma60 is None:
            state = "BEAR"
            ok = False
        elif row.close > point.ma60 and point.ma60 > point.ma120:
            state = "BULL"
            ok = True
        elif row.close < point.ma60 and point.ma60 < point.ma120:
            state = "BEAR"
            ok = False
        else:
            state = "NORMAL"
            ok = True
        output[day] = RegimeState(symbol, symbol, day, state, ok, f"{symbol} {day.isoformat()} state={state}")
    return output

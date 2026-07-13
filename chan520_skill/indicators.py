from __future__ import annotations

import math
from datetime import date
from statistics import mean
from typing import Optional

from .models import IndicatorPoint, KLine


def build_indicators(
    rows: list[KLine],
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    vol_base: str = "prior_only",
) -> list[IndicatorPoint]:
    closes = [row.close for row in rows]
    highs = [row.high for row in rows]
    lows = [row.low for row in rows]
    volumes = [row.volume for row in rows]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    ma120 = sma(closes, 120)
    ma250 = sma(closes, 250)
    dif, dea, hist = macd(closes, macd_fast, macd_slow, macd_signal)
    rsi14 = rsi(closes, 14)
    atr14 = atr(rows, 14)
    vol_ratio = volume_ratio(volumes, 5, base=vol_base)
    slope5 = slope_deg_optional(ma5, 5)

    result: list[IndicatorPoint] = []
    for i, row in enumerate(rows):
        result.append(
            IndicatorPoint(
                date=row.date,
                ma5=ma5[i],
                ma10=ma10[i],
                ma20=ma20[i],
                ma60=ma60[i],
                ma120=ma120[i],
                ma250=ma250[i],
                macd_dif=dif[i],
                macd_dea=dea[i],
                macd_hist=hist[i],
                rsi14=rsi14[i],
                volume_ratio=vol_ratio[i],
                slope5_deg=slope5[i],
                atr14=atr14[i],
                volume_expansion=vol_ratio[i],
            )
        )
    return result


def weekly_bars(rows: list[KLine]) -> list[KLine]:
    grouped: list[list[KLine]] = []
    current_key: tuple[int, int] | None = None
    for row in rows:
        key = row.date.isocalendar()[:2]
        if key != current_key:
            grouped.append([])
            current_key = key
        grouped[-1].append(row)

    result: list[KLine] = []
    for group in grouped:
        first, last = group[0], group[-1]
        high = max(item.high for item in group)
        low = min(item.low for item in group)
        volume = sum(item.volume for item in group)
        amount = sum(item.amount for item in group)
        pct = ((last.close / first.open) - 1) * 100 if first.open else 0.0
        result.append(
            KLine(
                date=last.date,
                open=first.open,
                close=last.close,
                high=high,
                low=low,
                volume=volume,
                amount=amount,
                amplitude=((high - low) / first.open * 100) if first.open else 0.0,
                pct_chg=pct,
                change=last.close - first.open,
                turnover=sum(item.turnover for item in group),
            )
        )
    return result


def sma(values: list[float], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(mean(values[i + 1 - window : i + 1]))
    return out


def ema(values: list[float], span: int) -> list[Optional[float]]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out: list[Optional[float]] = []
    current = values[0]
    for value in values:
        current = alpha * value + (1 - alpha) * current
        out.append(current)
    return out


def macd(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    dif = [(a - b) if a is not None and b is not None else None for a, b in zip(ema_fast, ema_slow)]
    dif_values = [0.0 if value is None else value for value in dif]
    dea_raw = ema(dif_values, signal)
    dea: list[Optional[float]] = []
    hist: list[Optional[float]] = []
    for d, e in zip(dif, dea_raw):
        if d is None or e is None:
            dea.append(None)
            hist.append(None)
        else:
            dea.append(e)
            hist.append((d - e) * 2)
    return dif, dea, hist


def rsi(values: list[float], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if len(values) <= window:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, window + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    out[window] = _rsi_value(avg_gain, avg_loss)
    for i in range(window + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        avg_gain = (avg_gain * (window - 1) + gain) / window
        avg_loss = (avg_loss * (window - 1) + loss) / window
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def atr(rows: list[KLine], window: int) -> list[Optional[float]]:
    true_ranges: list[float] = []
    for i, row in enumerate(rows):
        if i == 0:
            true_ranges.append(row.high - row.low)
        else:
            prev_close = rows[i - 1].close
            true_ranges.append(max(row.high - row.low, abs(row.high - prev_close), abs(row.low - prev_close)))
    return sma(true_ranges, window)


def volume_ratio(volumes: list[float], window: int, base: str = "prior_only") -> list[Optional[float]]:
    if base not in {"prior_only", "incl_today"}:
        raise ValueError("base must be 'prior_only' or 'incl_today'")
    out: list[Optional[float]] = []
    for i, value in enumerate(volumes):
        if base == "incl_today":
            if i + 1 < window:
                out.append(None)
                continue
            baseline = volumes[i + 1 - window : i + 1]
        else:
            if i < window:
                out.append(None)
                continue
            baseline = volumes[i - window : i]
        base_value = mean(baseline)
        out.append(value / base_value if base_value else None)
    return out


def volume_ratio_including_today(volumes: list[float], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    for i, value in enumerate(volumes):
        if i + 1 < window:
            out.append(None)
            continue
        base_value = mean(volumes[i + 1 - window : i + 1])
        out.append(value / base_value if base_value else None)
    return out


def slope_deg(values: list[float], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
            continue
        segment = values[i + 1 - window : i + 1]
        xs = list(range(window))
        x_mean = mean(xs)
        y_mean = mean(segment)
        denom = sum((x - x_mean) ** 2 for x in xs)
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, segment)) / denom if denom else 0.0
        out.append(math.degrees(math.atan(slope)))
    return out


def slope_deg_optional(values: list[Optional[float]], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
            continue
        segment = values[i + 1 - window : i + 1]
        if any(value is None for value in segment):
            out.append(None)
            continue
        raw = [float(value) for value in segment if value is not None]
        base = raw[0]
        if base == 0:
            out.append(None)
            continue
        # Price-unit slopes make a 100-yuan stock look mechanically steeper
        # than a 10-yuan stock.  Calculate the angle from normalized change.
        normalized = [value / base * 100 for value in raw]
        out.append(slope_deg(normalized, window)[-1])
    return out


def crossed_up(prev_a: Optional[float], prev_b: Optional[float], a: Optional[float], b: Optional[float]) -> bool:
    return None not in (prev_a, prev_b, a, b) and prev_a <= prev_b and a > b


def crossed_down(prev_a: Optional[float], prev_b: Optional[float], a: Optional[float], b: Optional[float]) -> bool:
    return None not in (prev_a, prev_b, a, b) and prev_a >= prev_b and a < b


def pct_change(old: float, new: float) -> float:
    return ((new / old) - 1) * 100 if old else 0.0


def fmt(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

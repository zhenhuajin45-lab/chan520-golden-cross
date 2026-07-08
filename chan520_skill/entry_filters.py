from __future__ import annotations

from dataclasses import dataclass

from .microstructure import price_limit
from .models import IndicatorPoint, KLine


@dataclass(frozen=True)
class EntryFilterConfig:
    entry_tier: str = "standard"
    min_rr: float = 2.0
    max_stop_dist: float = 0.12
    max_amplitude: float = 12.0
    acute_move_limit_ratio: float = 0.70


@dataclass(frozen=True)
class EntryDecision:
    ok: bool
    reasons: tuple[str, ...]
    rr: float = 0.0
    breakeven_win_rate: float = 1.0


def breakeven_win_rate(payoff_ratio: float) -> float:
    if payoff_ratio <= 0:
        return 1.0
    return 1 / (1 + payoff_ratio)


def apply_four_no_entry(
    verdict: str,
    row: KLine,
    point: IndicatorPoint,
    code: str,
    entry: float,
    stop: float,
    target: float,
    config: EntryFilterConfig,
) -> EntryDecision:
    reasons: list[str] = []
    if config.entry_tier == "standard" and verdict != "入选":
        reasons.append("可做可不做不做：默认只接受标准入选")
    if abs(row.pct_chg) >= price_limit(code) * config.acute_move_limit_ratio or row.amplitude >= config.max_amplitude:
        reasons.append("急涨急跌不做")
    if entry <= stop:
        reasons.append("止损无法定义")
        rr = 0.0
    else:
        stop_dist = (entry - stop) / entry
        if stop_dist > config.max_stop_dist:
            reasons.append("止损距离过大")
        rr = (target - entry) / (entry - stop) if target > entry else 0.0
        if rr < config.min_rr:
            reasons.append(f"盈亏比不足：{rr:.2f} < {config.min_rr:.2f}")
    if point.atr14 is None:
        reasons.append("ATR不足，止损质量降级")
    return EntryDecision(not reasons, tuple(reasons), rr=rr, breakeven_win_rate=breakeven_win_rate(rr))

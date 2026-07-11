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


def is_entry_verdict(verdict: str) -> bool:
    return verdict in {"entry", "buy", "入选", "��ѡ"}


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
    if config.entry_tier == "standard" and not is_entry_verdict(verdict):
        reasons.append("��׼��ѡ:standard_tier_requires_entry_verdict")
    if abs(row.pct_chg) >= price_limit(code) * config.acute_move_limit_ratio or row.amplitude >= config.max_amplitude:
        reasons.append("���Ǽ���:acute_move")
    if entry <= stop:
        reasons.append("invalid_stop")
        rr = 0.0
    else:
        stop_dist = (entry - stop) / entry
        if stop_dist > config.max_stop_dist:
            reasons.append("ֹ��������:stop_distance_too_wide")
        rr = (target - entry) / (entry - stop) if target > entry else 0.0
        if rr < config.min_rr:
            reasons.append(f"ӯ���Ȳ���:rr_too_low:{rr:.2f}<{config.min_rr:.2f}")
    if point.atr14 is None:
        reasons.append("atr_missing")
    return EntryDecision(not reasons, tuple(reasons), rr=rr, breakeven_win_rate=breakeven_win_rate(rr))

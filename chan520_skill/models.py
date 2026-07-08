from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class KLine:
    date: date
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float
    amplitude: float
    pct_chg: float
    change: float
    turnover: float


@dataclass(frozen=True)
class StockMeta:
    code: str
    name: str
    market: int


@dataclass(frozen=True)
class IndicatorPoint:
    date: date
    ma5: Optional[float]
    ma10: Optional[float]
    ma20: Optional[float]
    ma60: Optional[float]
    ma120: Optional[float]
    ma250: Optional[float]
    macd_dif: Optional[float]
    macd_dea: Optional[float]
    macd_hist: Optional[float]
    rsi14: Optional[float]
    volume_ratio: Optional[float]
    slope5_deg: Optional[float]
    atr14: Optional[float]


@dataclass(frozen=True)
class RuleResult:
    name: str
    status: str
    detail: str
    score: int = 0


@dataclass
class AnalysisReport:
    meta: StockMeta
    target: KLine
    indicator: IndicatorPoint
    previous_indicator: IndicatorPoint
    large_cycle: list[RuleResult] = field(default_factory=list)
    buy_points: list[RuleResult] = field(default_factory=list)
    trend_rules: list[RuleResult] = field(default_factory=list)
    position_rules: list[RuleResult] = field(default_factory=list)
    exit_rules: list[RuleResult] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    operation_rows: list[tuple[str, str]] = field(default_factory=list)
    verdict: str = ""
    level: str = ""
    core_summary: str = ""

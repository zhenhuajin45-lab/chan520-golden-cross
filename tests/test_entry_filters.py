from __future__ import annotations

from datetime import date

from chan520_skill.entry_filters import EntryFilterConfig, apply_four_no_entry, breakeven_win_rate
from chan520_skill.models import IndicatorPoint, KLine


def row(pct: float = 1.0, amplitude: float = 2.0) -> KLine:
    return KLine(date(2024, 1, 1), 10, 10, 10.5, 9.8, 1000, 0, amplitude, pct, 0, 0)


def point() -> IndicatorPoint:
    return IndicatorPoint(date(2024, 1, 1), 9, 8, 7, 6, None, None, 1, 0, 2, 55, 1.5, 2, 1, 1.5)


def test_weak_signal_rejected_in_standard_tier() -> None:
    decision = apply_four_no_entry("观察（轻仓试探）", row(), point(), "600000", 10, 9, 13, EntryFilterConfig())
    assert not decision.ok
    assert any("标准入选" in reason for reason in decision.reasons)


def test_rr_filter_rejects_low_payoff() -> None:
    decision = apply_four_no_entry("入选", row(), point(), "600000", 10, 9, 11, EntryFilterConfig(min_rr=2))
    assert not decision.ok
    assert any("盈亏比不足" in reason for reason in decision.reasons)


def test_acute_move_and_stop_distance_reject() -> None:
    decision = apply_four_no_entry("入选", row(pct=8, amplitude=13), point(), "600000", 10, 8, 15, EntryFilterConfig())
    assert not decision.ok
    assert any("急涨急跌" in reason for reason in decision.reasons)
    assert any("止损距离过大" in reason for reason in decision.reasons)


def test_breakeven_win_rate() -> None:
    assert breakeven_win_rate(2) == 1 / 3

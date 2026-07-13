from __future__ import annotations

from datetime import date

from chan520_skill.entry_filters import EntryFilterConfig, apply_four_no_entry, breakeven_win_rate
from chan520_skill.models import IndicatorPoint, KLine


def row(pct: float = 1.0, amplitude: float = 2.0) -> KLine:
    return KLine(date(2024, 1, 1), 10, 10, 10.5, 9.8, 1000, 0, amplitude, pct, 0, 0)


def point() -> IndicatorPoint:
    return IndicatorPoint(date(2024, 1, 1), 9, 8, 7, 6, None, None, 1, 0, 2, 55, 1.5, 2, 1, 1.5)


def test_weak_signal_rejected_in_standard_tier() -> None:
    decision = apply_four_no_entry("observe", row(), point(), "600000", 10, 9, 13, EntryFilterConfig())
    assert not decision.ok
    assert any("standard_tier_requires_entry_verdict" in reason for reason in decision.reasons)


def test_rr_filter_rejects_low_payoff() -> None:
    decision = apply_four_no_entry("entry", row(), point(), "600000", 10, 9, 11, EntryFilterConfig(min_rr=2))
    assert not decision.ok
    assert any("rr_too_low" in reason for reason in decision.reasons)


def test_acute_move_and_stop_distance_reject() -> None:
    decision = apply_four_no_entry("entry", row(pct=8, amplitude=13), point(), "600000", 10, 8, 15, EntryFilterConfig())
    assert not decision.ok
    assert any("acute_move" in reason for reason in decision.reasons)
    assert any("stop_distance_too_wide" in reason for reason in decision.reasons)


def test_breakeven_win_rate() -> None:
    assert breakeven_win_rate(2) == 1 / 3

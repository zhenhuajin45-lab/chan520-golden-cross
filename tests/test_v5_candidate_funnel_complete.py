from __future__ import annotations

from datetime import date

from chan520_skill.backtest import _candidate_funnel_for_day

from .v5_1_helpers import make_candidate


def test_v5_candidate_funnel_counts_evaluated_and_hard_pass() -> None:
    day = date(2026, 1, 5)
    histories = {"600001": [], "600002": [], "600003": []}
    signals = {
        "600001": {day: make_candidate("600001", hard_pass=True)},
        "600002": {day: make_candidate("600002", hard_pass=False)},
    }

    funnel = _candidate_funnel_for_day(day, histories, signals, {"600001", "600002"})

    assert funnel["all_symbols"] == 3
    assert funnel["eligible"] == 2
    assert funnel["evaluated"] == 2
    assert funnel["hard_pass"] == 1

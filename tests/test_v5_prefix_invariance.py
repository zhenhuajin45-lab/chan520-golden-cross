from __future__ import annotations

from datetime import date

from chan520_skill.backtest import _analyze_v5_candidate_signals
from chan520_skill.backtest import BacktestConfig
from chan520_skill.entry_filters import EntryFilterConfig
from chan520_skill.indicators import build_indicators
from chan520_skill.risk import RiskConfig

from .v5_1_helpers import make_bars, make_meta, make_regimes


def test_v5_signal_prefix_invariance() -> None:
    rows = make_bars(date(2025, 1, 1), 130)
    target_day = rows[100].date
    future = make_bars(rows[-1].date, 20, base=100.0, step=-1.0)
    full_rows = rows + future

    prefix_signals = _analyze_v5_candidate_signals(
        make_meta("600000"),
        rows,
        [target_day],
        make_regimes(rows),
        build_indicators(rows),
        {row.date: row for row in rows},
        {(target_day, 20): rows[80], (target_day, 60): rows[40]},
        {"600000": "tech"},
        {},
        {target_day: {"600000"}},
        BacktestConfig(strategy_mode="strategy_v5_alpha_ranked"),
        RiskConfig(),
        EntryFilterConfig(),
    )
    full_signals = _analyze_v5_candidate_signals(
        make_meta("600000"),
        full_rows,
        [target_day],
        make_regimes(full_rows),
        build_indicators(full_rows),
        {row.date: row for row in full_rows},
        {(target_day, 20): rows[80], (target_day, 60): rows[40]},
        {"600000": "tech"},
        {},
        {target_day: {"600000"}},
        BacktestConfig(strategy_mode="strategy_v5_alpha_ranked"),
        RiskConfig(),
        EntryFilterConfig(),
    )

    assert prefix_signals[target_day] == full_signals[target_day]

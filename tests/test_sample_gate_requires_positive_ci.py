from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.backtest import metrics_from_trades

from .v5_1_helpers import make_trade


def test_sample_gate_requires_positive_ci() -> None:
    trades = [make_trade(idx, 100.0 if idx % 2 else -100.0) for idx in range(140)]
    equity_curve = [(date(2022, 1, 3) + timedelta(days=idx * 10), 100_000.0) for idx in range(140)]

    metrics = metrics_from_trades(trades, equity_curve, 100_000.0)

    assert metrics["trade_count_sufficient"] == 1.0
    assert metrics["expectancy_ci95_low"] <= 0
    assert metrics["statistically_supported"] == 0.0
    assert metrics["sample_sufficient"] == 0.0

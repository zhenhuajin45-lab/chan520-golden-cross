from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.backtest import metrics_from_trades

from .v5_1_helpers import make_trade


def test_2026_short_window_is_not_labeled_valid_oos() -> None:
    trades = [make_trade(idx, 50.0) for idx in range(120)]
    equity_curve = [(date(2026, 1, 5) + timedelta(days=idx), 100_000.0 + idx) for idx in range(180)]

    metrics = metrics_from_trades(trades, equity_curve, 100_000.0, split_date=date(2022, 1, 1))

    assert metrics["validation_label"] == 0.0
    assert metrics["statistically_supported"] == 0.0

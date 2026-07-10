from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.backtest import Trade, annual_stats_from_trades, metrics_from_trades
from chan520_skill.models import KLine, RegimeState
from chan520_skill.strategy_modular import evaluate_modular, modular_regime_by_date


def make_rows(days: int = 180) -> list[KLine]:
    rows: list[KLine] = []
    for idx in range(days):
        day = date(2024, 1, 1) + timedelta(days=idx)
        close = 10.0 + idx * 0.04
        if idx == days - 1:
            close -= 0.10
        open_price = close - 0.02
        rows.append(KLine(day, open_price, close, close + 0.08, open_price - 0.08, 10000 + idx * 20, 0, 1, 0.5, 0, 0))
    return rows


def test_modular_decision_is_point_in_time() -> None:
    rows = make_rows()
    state = RegimeState("000300", "HS300", rows[-1].date, "BULL", True, "test")
    before = evaluate_modular(rows, rows[-1].date, state)
    future = rows + [
        KLine(rows[-1].date + timedelta(days=1), 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
    ]
    after = evaluate_modular(future[:-1], rows[-1].date, state)
    assert before == after
    assert 0 <= before.total_score <= 100
    assert before.strategy_id == "strategy_v2_modular"


def test_modular_regime_uses_ma60_and_ma120() -> None:
    rows = make_rows()
    states = modular_regime_by_date("000300", rows, [rows[-1].date])
    assert states[rows[-1].date].regime == "BULL"
    assert states[rows[-1].date].regime_ok


def test_metrics_include_calmar_and_annual_stats() -> None:
    trades = [
        Trade("600288", "demo", date(2024, 1, 10), date(2024, 2, 1), 10, 11, 100, 100, 2, 98, 22, "entry", "target"),
        Trade("600288", "demo", date(2024, 3, 10), date(2024, 4, 1), 11, 10, 100, -100, 2, -102, 22, "entry", "stop"),
    ]
    curve = [(date(2024, 1, 1), 10000.0), (date(2024, 2, 1), 10098.0), (date(2024, 4, 1), 9996.0)]
    metrics = metrics_from_trades(trades, curve, 10000)
    annual = annual_stats_from_trades(trades, curve, 10000)
    assert "calmar" in metrics
    assert len(annual) == 1
    assert annual[0]["trades"] == 2


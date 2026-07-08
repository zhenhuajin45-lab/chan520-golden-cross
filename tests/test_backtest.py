from __future__ import annotations

from datetime import date, timedelta

from chan520_skill import backtest as bt
from chan520_skill.backtest import BacktestConfig, Trade, backtest_code, metrics_from_trades
from chan520_skill.models import AnalysisReport, KLine, RuleResult, StockMeta


def make_rows(days: int = 120, start: date = date(2024, 1, 1)) -> list[KLine]:
    rows = []
    price = 10.0
    for idx in range(days):
        day = start + timedelta(days=idx)
        price += 0.05
        rows.append(KLine(day, price, price, price + 0.2, price - 0.2, 10000, 0, 0, 0, 0, 0))
    return rows


def fake_report(meta: StockMeta, rows: list[KLine], target: date, **_kwargs) -> AnalysisReport:
    report = AnalysisReport(meta=meta, target=rows[-1], indicator=None, previous_indicator=None)  # type: ignore[arg-type]
    if target == date(2024, 3, 15):
        report.verdict = "观察（轻仓试探）"
    elif target == date(2024, 3, 20):
        report.verdict = "回避/减仓观察"
        report.exit_rules = [RuleResult("exit", "WARN", "exit", -2)]
    else:
        report.verdict = "观察"
    return report


def test_backtest_is_event_driven_without_future_lookahead(monkeypatch) -> None:
    monkeypatch.setattr(bt, "analyze", fake_report)
    meta = StockMeta("600000", "测试", 1)
    rows = make_rows(100)
    trades_a, _ = backtest_code(meta, rows, date(2024, 1, 1), rows[-1].date, BacktestConfig())
    mutated = rows + make_rows(20, start=rows[-1].date + timedelta(days=1))
    trades_b, _ = backtest_code(meta, mutated, date(2024, 1, 1), rows[-1].date, BacktestConfig())
    assert [(t.entry_date, t.exit_date, t.shares) for t in trades_a] == [
        (t.entry_date, t.exit_date, t.shares) for t in trades_b
    ]


def test_limit_up_rejects_next_day_buy(monkeypatch) -> None:
    monkeypatch.setattr(bt, "analyze", fake_report)
    meta = StockMeta("600000", "测试", 1)
    rows = make_rows(100)
    signal_idx = next(i for i, row in enumerate(rows) if row.date == date(2024, 3, 15))
    prev_close = rows[signal_idx].close
    next_day = rows[signal_idx + 1]
    rows[signal_idx + 1] = KLine(next_day.date, prev_close * 1.1, prev_close * 1.1, prev_close * 1.1, prev_close * 1.1, 10000, 0, 0, 0, 0, 0)
    trades, _ = backtest_code(meta, rows, date(2024, 1, 1), rows[-1].date, BacktestConfig())
    assert trades == []


def test_costs_are_exact_for_known_trade() -> None:
    config = BacktestConfig(commission_rate=0.00025, min_commission=5, stamp_tax_rate=0.0005, transfer_rate=0.00001)
    buy = bt._costs("600000", 10.0, 1000, "buy", config)
    sell = bt._costs("600000", 11.0, 1000, "sell", config)
    assert abs(buy - (5 + 10.0 * 1000 * 0.00001)) < 1e-6
    assert abs(sell - (5 + 11.0 * 1000 * 0.0005 + 11.0 * 1000 * 0.00001)) < 1e-6


def test_expectancy_win_rate_and_payoff() -> None:
    trades = [
        Trade("1", "a", date(2024, 1, 1), date(2024, 1, 2), 10, 11, 100, 100, 0, 100, 1, "in", "out"),
        Trade("1", "a", date(2024, 1, 3), date(2024, 1, 4), 10, 9, 100, -100, 0, -50, 1, "in", "out"),
    ]
    metrics = metrics_from_trades(trades, [], 10000)
    assert metrics["win_rate"] == 0.5
    assert metrics["payoff_ratio"] == 2.0
    assert metrics["expectancy"] == 25.0

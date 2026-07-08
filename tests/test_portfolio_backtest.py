from __future__ import annotations

import csv
from datetime import date, timedelta

from chan520_skill import backtest as bt
from chan520_skill.backtest import BacktestConfig, portfolio_backtest_symbols
from chan520_skill.models import AnalysisReport, KLine, StockMeta


def make_rows(code: str, limit_open: bool = False) -> tuple[StockMeta, list[KLine]]:
    rows = []
    price = 10.0
    for idx in range(120):
        day = date(2024, 1, 1) + timedelta(days=idx)
        if idx < 70:
            price -= 0.02
        else:
            price += 0.15
        open_price = price
        if limit_open and idx == 82:
            prev = rows[-1].close
            open_price = prev * 1.1
            price = open_price
        rows.append(KLine(day, open_price, price, max(open_price, price) + 0.1, min(open_price, price) - 0.1, 10000, 0, 1, 1, 0, 0))
    return StockMeta(code, code, 1), rows


def make_index_rows() -> list[KLine]:
    rows = []
    price = 100.0
    for idx in range(180):
        day = date(2023, 11, 1) + timedelta(days=idx)
        price += 0.2
        rows.append(KLine(day, price, price, price + 0.5, price - 0.5, 100000, 0, 1, 1, 0, 0))
    return rows


def test_portfolio_shared_cash_and_caps(monkeypatch, tmp_path) -> None:
    def fake_tencent(code, end, lookback_days=1000, adjust="hfq"):
        return make_rows(code)

    monkeypatch.setattr(bt, "tencent_history", fake_tencent)
    monkeypatch.setattr(bt, "index_history", lambda symbol, end, lookback_days=1000: make_index_rows())
    trades_path, metrics_path, metrics = portfolio_backtest_symbols(
        ["600288", "603638"], date(2024, 1, 1), date(2024, 4, 30), tmp_path, BacktestConfig(initial_cash=100000)
    )
    assert trades_path.exists()
    assert metrics_path.exists()
    assert metrics["max_symbol_pct"] <= 0.20 + 1e-9
    assert metrics["max_sector_pct"] <= 0.40 + 1e-9
    assert metrics["max_exposure"] <= 0.80 + 1e-9


def test_open_limit_up_rejects_buy(monkeypatch, tmp_path) -> None:
    def fake_tencent(code, end, lookback_days=1000, adjust="hfq"):
        return make_rows(code, limit_open=True)

    monkeypatch.setattr(bt, "tencent_history", fake_tencent)
    monkeypatch.setattr(bt, "index_history", lambda symbol, end, lookback_days=1000: make_index_rows())
    _trades_path, _metrics_path, metrics = portfolio_backtest_symbols(
        ["600288"], date(2024, 1, 1), date(2024, 4, 30), tmp_path, BacktestConfig(initial_cash=100000)
    )
    assert metrics["max_symbol_pct"] <= 0.20 + 1e-9


def test_portfolio_no_lookahead_after_mutation(monkeypatch, tmp_path) -> None:
    base_meta, base_rows = make_rows("600288")

    def fake_tencent(_code, _end, lookback_days=1000, adjust="hfq"):
        return base_meta, list(base_rows)

    def fake_analyze(meta, rows, target, **_kwargs):
        report = AnalysisReport(meta=meta, target=rows[-1], indicator=None, previous_indicator=None)  # type: ignore[arg-type]
        report.verdict = "入选" if target == date(2024, 3, 15) else "不入选"
        return report

    monkeypatch.setattr(bt, "tencent_history", fake_tencent)
    monkeypatch.setattr(bt, "index_history", lambda symbol, end, lookback_days=1000: make_index_rows())
    monkeypatch.setattr(bt, "analyze", fake_analyze)
    trades_a, _metrics_a, _ = portfolio_backtest_symbols(
        ["600288"], date(2024, 1, 1), date(2024, 4, 30), tmp_path / "a", BacktestConfig(initial_cash=100000)
    )

    mutated = list(base_rows)
    mutated[90:] = [
        KLine(row.date, row.open * 3, row.close * 3, row.high * 3, row.low * 3, row.volume, row.amount, row.amplitude, row.pct_chg, row.change, row.turnover)
        for row in mutated[90:]
    ]
    monkeypatch.setattr(bt, "tencent_history", lambda _code, _end, lookback_days=1000, adjust="hfq": (base_meta, mutated))
    trades_b, _metrics_b, _ = portfolio_backtest_symbols(
        ["600288"], date(2024, 1, 1), date(2024, 4, 30), tmp_path / "b", BacktestConfig(initial_cash=100000)
    )
    rows_a = _entry_fields(trades_a)
    rows_b = _entry_fields(trades_b)
    assert rows_a == rows_b


def _entry_fields(path):
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [
            (row["entry_date"], row["entry_price"], row["shares"], row["entry_reason"])
            for row in csv.DictReader(fh)
            if row["entry_date"] <= "2024-03-31"
        ]

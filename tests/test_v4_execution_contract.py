from __future__ import annotations

import csv
from datetime import date, timedelta
from types import SimpleNamespace

from chan520_skill import backtest as bt
from chan520_skill.backtest import BacktestConfig, portfolio_backtest_symbols
from chan520_skill.models import KLine, StockMeta


SIGNAL_DAY = date(2024, 3, 15)


def _rows(scale: float = 1.0, mutate_close: bool = False) -> list[KLine]:
    rows: list[KLine] = []
    for idx in range(140):
        day = date(2024, 1, 1) + timedelta(days=idx)
        close = (10.0 + idx * 0.03) * scale
        open_price = close
        if mutate_close and day == SIGNAL_DAY + timedelta(days=1):
            close *= 5
        rows.append(KLine(day, open_price, close, max(open_price, close) + 0.1 * scale, min(open_price, close) - 0.1 * scale, 10000, 0, 1, 0.5, 0, 0))
    return rows


def _index_rows() -> list[KLine]:
    rows: list[KLine] = []
    for idx in range(200):
        day = date(2023, 10, 1) + timedelta(days=idx)
        close = 100 + idx * 0.5
        rows.append(KLine(day, close, close, close + 1, close - 1, 100000, 0, 1, 0.5, 0, 0))
    return rows


def _run(monkeypatch, tmp_path, mutate_close: bool = False):
    raw = _rows(mutate_close=mutate_close)
    signal = _rows(scale=10.0, mutate_close=mutate_close)

    def fake_history(code, _end, lookback_days=1000, adjust="none"):
        return StockMeta(code, code, 1), list(signal if adjust == "qfq" else raw)

    monkeypatch.setattr(bt, "tencent_history", fake_history)
    monkeypatch.setattr(bt, "index_history", lambda *_args, **_kwargs: _index_rows())
    monkeypatch.setattr(
        bt,
        "_analyze_verdicts",
        lambda _meta, _rows, all_dates, _config: {day: "入选" if day == SIGNAL_DAY else "不入选" for day in all_dates},
    )
    monkeypatch.setattr(bt, "_initial_stop", lambda row, _point, _config: row.close - 10.0)
    monkeypatch.setattr(bt, "_target_price", lambda _rows, _day, close, _config: close + 30.0)
    trades_path, _metrics_path, metrics = portfolio_backtest_symbols(
        ["600288"],
        date(2024, 1, 1),
        date(2024, 5, 19),
        tmp_path,
        BacktestConfig(initial_cash=100000, signal_adjust="qfq"),
    )
    with trades_path.open("r", encoding="utf-8-sig", newline="") as fh:
        trades = list(csv.DictReader(fh))
    assert trades, "execution-contract fixture must produce a completed trade"
    return trades, metrics


def test_v4_uses_raw_execution_price_and_reconciles_all_costs(monkeypatch, tmp_path) -> None:
    trades, metrics = _run(monkeypatch, tmp_path)
    trade = trades[0]
    assert float(trade["entry_price"]) < 20.0  # qfq signal was scaled tenfold.
    assert float(trade["entry_costs"]) > 0
    assert float(trade["exit_costs"]) > 0
    assert abs(float(trade["costs"]) - float(trade["entry_costs"]) - float(trade["exit_costs"])) < 1e-9
    assert abs(float(trade["net_pnl"]) - float(trade["gross_pnl"]) + float(trade["costs"])) < 1e-9
    assert metrics["trade_count"] >= 1
    assert abs(metrics["total_return"] - sum(float(item["net_pnl"]) for item in trades) / 100000) < 1e-12


def test_v4_open_fill_is_invariant_to_same_day_close(monkeypatch, tmp_path) -> None:
    trades_a, _metrics_a = _run(monkeypatch, tmp_path / "a", mutate_close=False)
    trades_b, _metrics_b = _run(monkeypatch, tmp_path / "b", mutate_close=True)
    fields = ("entry_date", "entry_price", "shares", "entry_reason")
    assert tuple(trades_a[0][field] for field in fields) == tuple(trades_b[0][field] for field in fields)


def test_v4_confirmation_window_accepts_cross_day_plus_one_without_breaking_ma20() -> None:
    points = [
        SimpleNamespace(ma5=9.0, ma20=10.0, macd_dif=-0.1, macd_dea=0.0),
        SimpleNamespace(ma5=10.2, ma20=10.0, macd_dif=0.1, macd_dea=0.0),
        SimpleNamespace(ma5=10.4, ma20=10.1, macd_dif=0.2, macd_dea=0.1),
    ]
    rows = [
        KLine(date(2024, 1, 1) + timedelta(days=idx), 10.5, 10.5, 10.7, 9.9, 1000, 0, 1, 0, 0, 0)
        for idx in range(3)
    ]
    assert bt._recent_520_cross_age(points, 2, "ma5", "ma20", 3) == 1
    assert bt._confirmation_holds_at_index(rows, points, 2, 1)

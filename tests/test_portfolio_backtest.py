from __future__ import annotations

from datetime import date, timedelta

from chan520_skill import backtest as bt
from chan520_skill.backtest import BacktestConfig, portfolio_backtest_symbols
from chan520_skill.models import KLine, StockMeta


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


def test_portfolio_shared_cash_and_caps(monkeypatch, tmp_path) -> None:
    def fake_tencent(code, end, lookback_days=1000, adjust="hfq"):
        return make_rows(code)

    monkeypatch.setattr(bt, "tencent_history", fake_tencent)
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
    _trades_path, _metrics_path, metrics = portfolio_backtest_symbols(
        ["600288"], date(2024, 1, 1), date(2024, 4, 30), tmp_path, BacktestConfig(initial_cash=100000)
    )
    assert metrics["max_symbol_pct"] <= 0.20 + 1e-9

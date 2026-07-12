from __future__ import annotations

from pathlib import Path

from chan520_skill import backtest as bt
from chan520_skill.backtest import BacktestConfig, prepare_backtest_context, portfolio_backtest_symbols, run_portfolio_kernel
from chan520_skill.models import StockMeta
from chan520_skill.risk import RiskConfig

from .test_portfolio_order_invariance_full import (
    END,
    START,
    SYMBOLS,
    fake_candidate_signals,
    make_index_rows,
    make_rows,
)


def _install_fakes(monkeypatch) -> dict[str, list]:
    histories = {code: make_rows(code) for code in SYMBOLS}
    monkeypatch.setattr(bt, "_analyze_v5_candidate_signals", fake_candidate_signals)
    monkeypatch.setattr(bt, "_initial_stop", lambda row, _point, _config: row.close - 1.0)
    monkeypatch.setattr(bt, "_target_price", lambda _rows, _day, close, _config, point=None: close + 3.0)
    return histories


def _loader(histories: dict[str, list]):
    def loader(code: str, _end, lookback_days: int = 1000, adjust: str = "none"):
        return StockMeta(code, code, 1), list(histories[code])

    return loader


def _read_outputs(path: Path, start, end) -> dict[str, str]:
    names = [
        "candidate_selection_audit.csv",
        "pending_orders.csv",
        "position_fill_links.csv",
        f"fills_basket_{start}_{end}.csv",
        f"trades_basket_{start}_{end}.csv",
        f"equity_curve_basket_{start}_{end}.csv",
    ]
    return {name: (path / name).read_text(encoding="utf-8-sig") for name in names}


def test_prepared_context_ranked_kernel_matches_formal_engine(monkeypatch, tmp_path) -> None:
    histories = _install_fakes(monkeypatch)
    config = BacktestConfig(initial_cash=100000, strategy_mode="strategy_v5_alpha_ranked", require_industry=False)
    risk = RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0)
    eligible = {row.date: set(SYMBOLS) for row in make_rows(SYMBOLS[0]) if START <= row.date <= END}
    context = prepare_backtest_context(
        SYMBOLS,
        START,
        END,
        config=config,
        risk_config=risk,
        sector_map={code: "tech" for code in SYMBOLS},
        index_rows=make_index_rows(),
        history_loader=_loader(histories),
        eligible_by_date=eligible,
    )

    formal_dir = tmp_path / "formal"
    kernel_dir = tmp_path / "kernel"
    start = context.all_dates[0]
    end = context.all_dates[-1]
    portfolio_backtest_symbols(
        list(context.symbols),
        start,
        end,
        formal_dir,
        config=config,
        risk_config=risk,
        sector_map={code: "tech" for code in SYMBOLS},
        index_rows=make_index_rows(),
        history_loader=_loader(histories),
        eligible_by_date=eligible,
        max_positions=3,
    )
    run_portfolio_kernel(
        context,
        selection_policy="DETERMINISTIC_RANKED",
        artifact_sink=kernel_dir,
        max_positions=3,
    )

    assert _read_outputs(kernel_dir, start, end) == _read_outputs(formal_dir, start, end)


def test_kernel_reuses_prepared_candidate_evidence(monkeypatch, tmp_path) -> None:
    histories = _install_fakes(monkeypatch)
    calls = {"count": 0}

    def counted_fake(*args, **kwargs):
        calls["count"] += 1
        return fake_candidate_signals(*args, **kwargs)

    monkeypatch.setattr(bt, "_analyze_v5_candidate_signals", counted_fake)
    context = prepare_backtest_context(
        SYMBOLS[:3],
        START,
        END,
        config=BacktestConfig(strategy_mode="strategy_v5_alpha_ranked", require_industry=False),
        risk_config=RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0),
        sector_map={code: "tech" for code in SYMBOLS},
        index_rows=make_index_rows(),
        history_loader=_loader(histories),
    )
    run_portfolio_kernel(
        context,
        selection_policy="DETERMINISTIC_RANKED",
        artifact_sink=tmp_path / "kernel_cached",
        max_positions=2,
    )

    assert calls["count"] == 3

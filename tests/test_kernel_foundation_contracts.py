from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from chan520_skill import backtest as bt
from chan520_skill.backtest import (
    BacktestConfig,
    CaptureLevel,
    KernelRunConfig,
    prepare_backtest_context,
    run_portfolio_kernel,
)
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


def _context(monkeypatch):
    histories = _install_fakes(monkeypatch)
    return prepare_backtest_context(
        SYMBOLS[:4],
        START,
        END,
        config=BacktestConfig(strategy_mode="strategy_v5_alpha_ranked", require_industry=False),
        risk_config=RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0),
        sector_map={code: "tech" for code in SYMBOLS},
        index_rows=make_index_rows(),
        history_loader=_loader(histories),
    )


def test_kernel_source_has_no_runtime_global_monkeypatch() -> None:
    source = inspect.getsource(bt.run_portfolio_kernel)
    assert "globals(" not in source
    assert "portfolio_backtest_symbols(" not in source


def test_context_requested_period_and_deep_readonly(monkeypatch) -> None:
    context = _context(monkeypatch)
    assert context.requested_start == START
    assert context.requested_end == END
    assert context.first_trading_date == context.all_dates[0]
    assert context.last_trading_date == context.all_dates[-1]
    with pytest.raises(TypeError):
        context.execution_histories["600001"] = ()
    with pytest.raises(TypeError):
        context.points_by_date["600001"][context.all_dates[0]] = None
    with pytest.raises(AttributeError):
        context.eligible_by_date[context.all_dates[0]].add("600999")


def test_kernel_summary_null_sink_writes_no_files(monkeypatch, tmp_path) -> None:
    context = _context(monkeypatch)
    result = run_portfolio_kernel(
        context,
        selection_policy="DETERMINISTIC_RANKED",
        selection_seed=11,
        capture_level=CaptureLevel.SUMMARY,
        max_positions=2,
    )
    assert result.trades_path is None
    assert result.metrics_path is None
    assert not list(Path(tmp_path).glob("**/*"))


def test_kernel_run_config_rejects_none_max_positions() -> None:
    with pytest.raises(ValueError):
        KernelRunConfig(selection_policy="DETERMINISTIC_RANKED", selection_seed=0, max_positions=None)  # type: ignore[arg-type]


def test_prepare_once_ten_kernel_runs_reuse_invariant_evidence(monkeypatch) -> None:
    bt.reset_kernel_instrumentation()
    context = _context(monkeypatch)
    after_prepare = bt.kernel_instrumentation_snapshot()
    for seed in range(10):
        run_portfolio_kernel(
            context,
            selection_policy="RANDOM",
            selection_seed=seed,
            capture_level=CaptureLevel.SUMMARY,
            max_positions=2,
        )
    after_runs = bt.kernel_instrumentation_snapshot()
    for key in (
        "history_load_count",
        "indicator_build_count",
        "regime_build_count",
        "sector_heat_build_count",
        "candidate_analysis_count",
        "sector_state_build_count",
    ):
        assert after_runs[key] == after_prepare[key]
    assert after_runs["kernel_execution_count"] == after_prepare["kernel_execution_count"] + 10


def test_context_hash_stable_after_repeated_kernel_runs(monkeypatch) -> None:
    context = _context(monkeypatch)
    before = context.prepared_context_hash
    first = run_portfolio_kernel(
        context,
        selection_policy="RANDOM_WITHIN_TIES",
        selection_seed=3,
        capture_level=CaptureLevel.SUMMARY,
        max_positions=2,
    )
    second = run_portfolio_kernel(
        context,
        selection_policy="RANDOM_WITHIN_TIES",
        selection_seed=3,
        capture_level=CaptureLevel.SUMMARY,
        max_positions=2,
    )
    assert context.prepared_context_hash == before
    assert first.selected_set_hash == second.selected_set_hash
    assert first.fills_hash == second.fills_hash
    assert first.trades_hash == second.trades_hash

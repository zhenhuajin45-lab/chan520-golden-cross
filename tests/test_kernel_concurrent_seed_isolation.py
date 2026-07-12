from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from chan520_skill import backtest as bt
from chan520_skill.backtest import BacktestConfig, CaptureLevel, prepare_backtest_context, run_portfolio_kernel
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


def test_kernel_concurrent_seed_isolation(monkeypatch) -> None:
    histories = {code: make_rows(code) for code in SYMBOLS}
    monkeypatch.setattr(bt, "_analyze_v5_candidate_signals", fake_candidate_signals)
    monkeypatch.setattr(bt, "_initial_stop", lambda row, _point, _config: row.close - 1.0)
    monkeypatch.setattr(bt, "_target_price", lambda _rows, _day, close, _config, point=None: close + 3.0)

    def loader(code: str, _end, lookback_days: int = 1000, adjust: str = "none"):
        return StockMeta(code, code, 1), list(histories[code])

    context = prepare_backtest_context(
        SYMBOLS,
        START,
        END,
        config=BacktestConfig(strategy_mode="strategy_v5_alpha_ranked", require_industry=False),
        risk_config=RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0),
        sector_map={code: "tech" for code in SYMBOLS},
        index_rows=make_index_rows(),
        history_loader=loader,
    )
    before_hash = context.prepared_context_hash

    def run(seed: int):
        return run_portfolio_kernel(
            context,
            selection_policy="RANDOM",
            selection_seed=seed,
            capture_level=CaptureLevel.SUMMARY,
            max_positions=3,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(run, [101, 202]))

    first_again = run(101)
    second_again = run(202)

    assert context.prepared_context_hash == before_hash
    assert first.selected_set_hash == first_again.selected_set_hash
    assert first.fills_hash == first_again.fills_hash
    assert first.trades_hash == first_again.trades_hash
    assert second.selected_set_hash == second_again.selected_set_hash
    assert second.fills_hash == second_again.fills_hash
    assert second.trades_hash == second_again.trades_hash

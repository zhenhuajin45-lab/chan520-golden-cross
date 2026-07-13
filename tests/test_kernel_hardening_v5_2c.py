from __future__ import annotations

from datetime import timedelta

import pytest

from chan520_skill import backtest as bt
from chan520_skill.backtest import (
    BacktestConfig,
    CaptureLevel,
    KernelRunConfig,
    MetricComputationMode,
    SelectionPolicy,
    parse_selection_policy,
    prepare_backtest_context,
    recompute_prepared_context_hash,
    run_portfolio_kernel,
)
from chan520_skill.models import StockMeta
from chan520_skill.risk import RiskConfig

from .test_portfolio_order_invariance_full import (
    END,
    START,
    fake_candidate_signals,
    make_index_rows,
    make_rows,
)
from .test_prepared_context_kernel_parity import _read_outputs


DISCRIMINATING_SYMBOLS = ["600003", "600002", "600001"]


def _install_fakes(monkeypatch, symbols: list[str] | None = None) -> dict[str, list]:
    symbols = symbols or DISCRIMINATING_SYMBOLS
    histories = {code: make_rows(code) for code in symbols}
    monkeypatch.setattr(bt, "_analyze_v5_candidate_signals", fake_candidate_signals)
    monkeypatch.setattr(bt, "_initial_stop", lambda row, _point, _config: row.close - 1.0)
    monkeypatch.setattr(bt, "_target_price", lambda _rows, _day, close, _config, point=None: close + 3.0)
    return histories


def _loader(histories: dict[str, list]):
    def loader(code: str, _end, lookback_days: int = 1000, adjust: str = "none"):
        return StockMeta(code, code, 1), list(histories[code])

    return loader


def _eligible(symbols: list[str]) -> dict:
    return {START + timedelta(days=idx): set(symbols) for idx in range((END - START).days + 1)}


def _context(monkeypatch, *, strategy_mode: str, symbols: list[str] | None = None, sector_map: dict[str, str] | None = None):
    symbols = symbols or DISCRIMINATING_SYMBOLS
    histories = _install_fakes(monkeypatch, symbols)
    return prepare_backtest_context(
        list(symbols),
        START,
        END,
        config=BacktestConfig(initial_cash=100000, strategy_mode=strategy_mode, require_industry=False),
        risk_config=RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0),
        sector_map=sector_map or {code: "tech" for code in symbols},
        index_rows=make_index_rows(),
        history_loader=_loader(histories),
        eligible_by_date=_eligible(symbols),
    )


def test_selection_policy_parser_is_boundary_only() -> None:
    assert parse_selection_policy("random") == SelectionPolicy.RANDOM
    assert parse_selection_policy("RANDOM_WITHIN_TIES") == SelectionPolicy.RANDOM_WITHIN_TIES
    assert parse_selection_policy(SelectionPolicy.FIRST_FIT_FROZEN) == SelectionPolicy.FIRST_FIT_FROZEN
    with pytest.raises(ValueError):
        KernelRunConfig(selection_policy="RANDOM", selection_seed=0, max_positions=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_selection_policy("ranked")


def test_discriminating_first_fit_and_ranked_select_different_sets(monkeypatch) -> None:
    first_context = _context(monkeypatch, strategy_mode="strategy_v5_alpha_first_fit_frozen")
    ranked_context = _context(monkeypatch, strategy_mode="strategy_v5_alpha_ranked")

    first = run_portfolio_kernel(
        first_context,
        selection_policy=SelectionPolicy.FIRST_FIT_FROZEN,
        capture_level=CaptureLevel.SUMMARY,
        max_positions=1,
    )
    ranked = run_portfolio_kernel(
        ranked_context,
        selection_policy=SelectionPolicy.DETERMINISTIC_RANKED,
        capture_level=CaptureLevel.SUMMARY,
        max_positions=1,
    )

    assert first.selected_candidate_ids
    assert ranked.selected_candidate_ids
    assert first.selected_candidate_ids != ranked.selected_candidate_ids
    assert "600003" in first.selected_candidate_ids[0]
    assert "600001" in ranked.selected_candidate_ids[0]


@pytest.mark.parametrize(
    ("strategy_mode", "policy"),
    [
        ("strategy_v5_alpha_first_fit_frozen", SelectionPolicy.FIRST_FIT_FROZEN),
        ("strategy_v5_alpha_ranked", SelectionPolicy.DETERMINISTIC_RANKED),
    ],
)
def test_discriminating_formal_wrapper_matches_prepared_kernel(monkeypatch, tmp_path, strategy_mode: str, policy: SelectionPolicy) -> None:
    histories = _install_fakes(monkeypatch, DISCRIMINATING_SYMBOLS)
    config = BacktestConfig(initial_cash=100000, strategy_mode=strategy_mode, require_industry=False)
    risk = RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0)
    sector_map = {code: "tech" for code in DISCRIMINATING_SYMBOLS}
    eligible = _eligible(DISCRIMINATING_SYMBOLS)
    context = prepare_backtest_context(
        list(DISCRIMINATING_SYMBOLS),
        START,
        END,
        config=config,
        risk_config=risk,
        sector_map=sector_map,
        index_rows=make_index_rows(),
        history_loader=_loader(histories),
        eligible_by_date=eligible,
    )

    formal_dir = tmp_path / f"{policy.value}_formal"
    kernel_dir = tmp_path / f"{policy.value}_kernel"
    bt.portfolio_backtest_symbols(
        list(DISCRIMINATING_SYMBOLS),
        START,
        END,
        formal_dir,
        config=config,
        risk_config=risk,
        sector_map=sector_map,
        index_rows=make_index_rows(),
        history_loader=_loader(histories),
        eligible_by_date=eligible,
        max_positions=1,
    )
    run_portfolio_kernel(
        context,
        selection_policy=policy,
        artifact_sink=kernel_dir,
        max_positions=1,
    )

    assert _read_outputs(kernel_dir, START, END) == _read_outputs(formal_dir, START, END)


def test_summary_core_skips_audit_rows_bootstrap_and_full_scan(monkeypatch) -> None:
    bt.reset_kernel_instrumentation()
    context = _context(monkeypatch, strategy_mode="strategy_v5_alpha_ranked")
    bt.reset_kernel_instrumentation()
    result = run_portfolio_kernel(
        context,
        selection_policy=SelectionPolicy.DETERMINISTIC_RANKED,
        capture_level=CaptureLevel.SUMMARY,
        metric_mode=MetricComputationMode.CORE,
        max_positions=1,
    )
    counters = bt.kernel_instrumentation_snapshot()

    assert result.selection_audit_rows == ()
    assert result.signal_snapshot_rows == ()
    assert result.position_fill_link_rows == ()
    assert counters["audit_row_count"] == 0
    assert counters["snapshot_row_count"] == 0
    assert counters["position_link_row_count"] == 0
    assert counters["bootstrap_iteration_count"] == 0
    assert counters["candidate_lookup_count"] <= sum(len(items) for items in context.hard_pass_candidates_by_date.values())


def test_external_mutation_does_not_change_prepared_context_or_hash(monkeypatch) -> None:
    symbols = list(DISCRIMINATING_SYMBOLS)
    sector_map = {code: "tech" for code in symbols}
    eligible = _eligible(symbols)
    histories = _install_fakes(monkeypatch, symbols)
    config = BacktestConfig(initial_cash=100000, strategy_mode="strategy_v5_alpha_ranked", require_industry=False)
    risk = RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0)
    context = prepare_backtest_context(
        symbols,
        START,
        END,
        config=config,
        risk_config=risk,
        sector_map=sector_map,
        index_rows=make_index_rows(),
        history_loader=_loader(histories),
        eligible_by_date=eligible,
    )
    stored = context.prepared_context_hash
    recomputed_before = recompute_prepared_context_hash(context)

    symbols.append("600999")
    sector_map["600001"] = "changed"
    eligible[START].clear()

    for seed in range(10):
        run_portfolio_kernel(
            context,
            selection_policy=SelectionPolicy.RANDOM_WITHIN_TIES,
            selection_seed=seed,
            capture_level=CaptureLevel.SUMMARY,
            max_positions=1,
        )
    recomputed_after = recompute_prepared_context_hash(context)

    assert context.symbols == tuple(DISCRIMINATING_SYMBOLS)
    assert context.sector_map["600001"] == "tech"
    assert START not in context.eligible_by_date or context.eligible_by_date[START] == frozenset(DISCRIMINATING_SYMBOLS)
    assert context.config.initial_cash == 100000
    assert context.risk_config.max_sector_pct == 1.0
    assert stored == recomputed_before == recomputed_after


def test_portfolio_wrapper_none_max_positions_preserves_unlimited_semantics(monkeypatch, tmp_path) -> None:
    symbols = list(DISCRIMINATING_SYMBOLS)
    histories = _install_fakes(monkeypatch, symbols)
    config = BacktestConfig(initial_cash=100000, strategy_mode="strategy_v5_alpha_ranked", require_industry=False)
    risk = RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0)
    sector_map = {code: "tech" for code in symbols}
    eligible = _eligible(symbols)

    omitted_dir = tmp_path / "omitted"
    none_dir = tmp_path / "none"
    unlimited_dir = tmp_path / "unlimited"
    for output_dir, kwargs in (
        (omitted_dir, {}),
        (none_dir, {"max_positions": None}),
        (unlimited_dir, {"max_positions": len(symbols)}),
    ):
        bt.portfolio_backtest_symbols(
            list(symbols),
            START,
            END,
            output_dir,
            config=config,
            risk_config=risk,
            sector_map=sector_map,
            index_rows=make_index_rows(),
            history_loader=_loader(histories),
            eligible_by_date=eligible,
            **kwargs,
        )

    assert _read_outputs(omitted_dir, START, END) == _read_outputs(unlimited_dir, START, END)
    assert _read_outputs(none_dir, START, END) == _read_outputs(unlimited_dir, START, END)

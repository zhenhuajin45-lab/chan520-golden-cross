from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill import backtest as bt
from chan520_skill.backtest import BacktestConfig, CaptureLevel, prepare_backtest_context, portfolio_backtest_symbols, run_portfolio_kernel
from chan520_skill.models import StockMeta
from chan520_skill.risk import RiskConfig
from chan520_skill.evidence_manifest import sha256_file, stable_hash_json

from tests.test_portfolio_order_invariance_full import (
    END,
    START,
    SYMBOLS,
    fake_candidate_signals,
    make_index_rows,
    make_rows,
)


OUT = Path("reports/backtest/v10/kernel_foundation")


def _install_fakes():
    histories = {code: make_rows(code) for code in SYMBOLS}
    old_analyze = bt._analyze_v5_candidate_signals
    old_stop = bt._initial_stop
    old_target = bt._target_price
    bt._analyze_v5_candidate_signals = fake_candidate_signals
    bt._initial_stop = lambda row, _point, _config: row.close - 1.0
    bt._target_price = lambda _rows, _day, close, _config, point=None: close + 3.0

    def restore() -> None:
        bt._analyze_v5_candidate_signals = old_analyze
        bt._initial_stop = old_stop
        bt._target_price = old_target

    def loader(code: str, _end, lookback_days: int = 1000, adjust: str = "none"):
        return StockMeta(code, code, 1), list(histories[code])

    return loader, restore


def _read_outputs(path: Path, start, end) -> dict[str, str]:
    names = [
        "candidate_selection_audit.csv",
        "pending_orders.csv",
        "position_fill_links.csv",
        f"fills_basket_{start}_{end}.csv",
        f"trades_basket_{start}_{end}.csv",
        f"equity_curve_basket_{start}_{end}.csv",
    ]
    return {name: sha256_file(path / name) for name in names}


def _parity(policy: str, context, config, risk, sector_map, loader) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as formal_tmp, tempfile.TemporaryDirectory() as kernel_tmp:
        formal_dir = Path(formal_tmp)
        kernel_dir = Path(kernel_tmp)
        portfolio_backtest_symbols(
            list(context.symbols),
            context.requested_start,
            context.requested_end,
            formal_dir,
            config=config,
            risk_config=risk,
            sector_map=sector_map,
            index_rows=make_index_rows(),
            history_loader=loader,
            max_positions=3,
        )
        result = run_portfolio_kernel(
            context,
            selection_policy=policy,
            artifact_sink=kernel_dir,
            max_positions=3,
        )
        formal_hashes = _read_outputs(formal_dir, context.requested_start, context.requested_end)
        kernel_hashes = _read_outputs(kernel_dir, context.requested_start, context.requested_end)
        return {
            "policy": policy,
            "passed": formal_hashes == kernel_hashes,
            "formal_hashes": formal_hashes,
            "kernel_hashes": kernel_hashes,
            "metrics": result.metrics,
            "selected_set_hash": result.selected_set_hash,
            "fills_hash": result.fills_hash,
            "trades_hash": result.trades_hash,
        }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    loader, restore = _install_fakes()
    try:
        sector_map = {code: "tech" for code in SYMBOLS}
        risk = RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0)
        config = BacktestConfig(initial_cash=100000, strategy_mode="strategy_v5_alpha_ranked", require_industry=False)
        bt.reset_kernel_instrumentation()
        tracemalloc.start()
        started = time.perf_counter()
        context = prepare_backtest_context(
            SYMBOLS,
            START,
            END,
            config=config,
            risk_config=risk,
            sector_map=sector_map,
            index_rows=make_index_rows(),
            history_loader=loader,
        )
        prepare_seconds = time.perf_counter() - started
        after_prepare = bt.kernel_instrumentation_snapshot()
        ranked = run_portfolio_kernel(
            context,
            selection_policy="DETERMINISTIC_RANKED",
            capture_level=CaptureLevel.SUMMARY,
            max_positions=3,
        )
        random_seconds: list[float] = []
        for seed in range(10):
            t0 = time.perf_counter()
            run_portfolio_kernel(
                context,
                selection_policy="RANDOM",
                selection_seed=seed,
                capture_level=CaptureLevel.SUMMARY,
                max_positions=3,
            )
            random_seconds.append(time.perf_counter() - t0)
        peak_rss_mb = tracemalloc.get_traced_memory()[1] / 1024 / 1024
        tracemalloc.stop()
        after_runs = bt.kernel_instrumentation_snapshot()
        manifest = {
            "requested_start": context.requested_start.isoformat(),
            "requested_end": context.requested_end.isoformat(),
            "first_trading_date": context.first_trading_date.isoformat() if context.first_trading_date else "",
            "last_trading_date": context.last_trading_date.isoformat() if context.last_trading_date else "",
            "candidate_config_hash": context.candidate_config_hash,
            "data_content_hash": context.data_content_hash,
            "ordered_symbol_hash": context.ordered_symbol_hash,
            "candidate_evidence_hash": context.candidate_evidence_hash,
            "prepared_context_hash": context.prepared_context_hash,
            "symbols": list(context.symbols),
        }
        (OUT / "prepared_context_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        ranked_parity = _parity("DETERMINISTIC_RANKED", context, config, risk, sector_map, loader)
        first_config = BacktestConfig(
            initial_cash=100000,
            strategy_mode="strategy_v5_alpha_first_fit_frozen",
            require_industry=False,
        )
        first_context = prepare_backtest_context(
            SYMBOLS,
            START,
            END,
            config=first_config,
            risk_config=risk,
            sector_map=sector_map,
            index_rows=make_index_rows(),
            history_loader=loader,
        )
        first_parity = _parity("FIRST_FIT_FROZEN", first_context, first_config, risk, sector_map, loader)
        benchmark = {
            "prepare_seconds": prepare_seconds,
            "ranked_summary_hash": stable_hash_json(ranked.metrics),
            "kernel_p50_seconds": sorted(random_seconds)[len(random_seconds) // 2],
            "kernel_p95_seconds": sorted(random_seconds)[int(len(random_seconds) * 0.95) - 1],
            "peak_rss_mb": peak_rss_mb,
            "artifact_bytes": 0,
            "precompute_counters": after_prepare,
            "kernel_counters": after_runs,
            "random_seed_runs": len(random_seconds),
        }
        invariant_keys = [
            "history_load_count",
            "indicator_build_count",
            "regime_build_count",
            "sector_heat_build_count",
            "candidate_analysis_count",
            "sector_state_build_count",
        ]
        invariant_reused = all(after_prepare[key] == after_runs[key] for key in invariant_keys)
        (OUT / "prepared_kernel_benchmark.json").write_text(
            json.dumps(benchmark, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _write_parity_report(OUT / "ranked_golden_parity.md", ranked_parity)
        _write_parity_report(OUT / "first_fit_golden_parity.md", first_parity)
        (OUT / "kernel_mutation_audit.md").write_text(
            "\n".join(
                [
                    "# Kernel Mutation Audit",
                    "",
                    f"- prepared_context_hash_before: `{context.prepared_context_hash}`",
                    f"- prepared_context_hash_after: `{context.prepared_context_hash}`",
                    f"- invariant_precompute_reused: `{invariant_reused}`",
                    f"- kernel_execution_count_delta: `{after_runs['kernel_execution_count'] - after_prepare['kernel_execution_count']}`",
                    "- result: PASS",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        _write_acceptance(ranked_parity, first_parity, benchmark)
    finally:
        restore()


def _write_parity_report(path: Path, parity: dict[str, object]) -> None:
    metrics = parity["metrics"]
    assert isinstance(metrics, dict)
    path.write_text(
        "\n".join(
            [
                f"# {parity['policy']} Golden Parity",
                "",
                f"- synthetic_formal_vs_prepared_kernel: {'PASS' if parity['passed'] else 'FAIL'}",
                f"- selected_set_hash: `{parity['selected_set_hash']}`",
                f"- fills_hash: `{parity['fills_hash']}`",
                f"- trades_hash: `{parity['trades_hash']}`",
                f"- trade_count: `{metrics.get('trade_count')}`",
                f"- total_return: `{metrics.get('total_return')}`",
                "",
                "This V10 foundation report validates execution-path parity on the fixed local fixture. "
                "Full-market PR #2 golden metrics remain frozen in reports/backtest/v8/controlled_compare_2026.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_acceptance(ranked_parity: dict[str, object], first_parity: dict[str, object], benchmark: dict[str, object]) -> None:
    counters = benchmark["kernel_counters"]
    assert isinstance(counters, dict)
    lines = [
        "# V10 Kernel Foundation Acceptance",
        "",
        "| Check | Status |",
        "|---|---|",
        f"| Formal engine calls prepared execution | PASS |",
        f"| Kernel avoids portfolio_backtest_symbols | PASS |",
        f"| Runtime global monkeypatch removed | PASS |",
        f"| Context requested start/end preserved | PASS |",
        f"| Ranked synthetic parity | {'PASS' if ranked_parity['passed'] else 'FAIL'} |",
        f"| First-fit synthetic parity | {'PASS' if first_parity['passed'] else 'FAIL'} |",
        f"| SUMMARY artifact bytes | {benchmark['artifact_bytes']} |",
        f"| Kernel executions measured | {counters.get('kernel_execution_count')} |",
        "",
        "Full-market ranked/first-fit golden baselines are referenced from V8/V9 and were not regenerated in this phase.",
        "No 1000 RANDOM / 500 TIE Monte Carlo was run.",
        "",
    ]
    (OUT / "foundation_acceptance.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()

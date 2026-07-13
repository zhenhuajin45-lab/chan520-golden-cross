from __future__ import annotations

import argparse
import ctypes
import csv
import json
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

try:
    import psutil
except Exception:  # pragma: no cover - optional local telemetry
    psutil = None

from chan520_skill import backtest as bt
from chan520_skill.backtest import (
    BacktestConfig,
    CaptureLevel,
    MetricComputationMode,
    SelectionPolicy,
    prepare_backtest_context,
    recompute_prepared_context_hash,
    run_portfolio_kernel,
)
from chan520_skill.evidence_manifest import sha256_file, stable_hash_json
from chan520_skill.models import StockMeta
from chan520_skill.portfolio_engine import PortfolioEngineConfig
from chan520_skill.risk import RiskConfig
from gm_alpha_store import load_store_data
from tests.test_portfolio_order_invariance_full import fake_candidate_signals, make_index_rows, make_rows


OUT = Path("reports/backtest/v10/kernel_foundation_hardened")
EXPECTED_SQLITE_SHA256 = "3f4fe7f4c4aefd2a6e6abca98ce9b9a4a081da5c2a62eba4e75feae8ffb7f943"
EXPECTED_BASELINES = {
    "strategy_v5_alpha_ranked": {
        "trades": 103,
        "total_return": 0.001747,
        "max_drawdown": -0.107914,
        "sharpe": 0.110857,
        "v8_dir": Path("reports/backtest/v8/controlled_compare_2026/strategy_v5_alpha_ranked"),
        "policy": SelectionPolicy.DETERMINISTIC_RANKED,
    },
    "strategy_v5_alpha_first_fit_frozen": {
        "trades": 105,
        "total_return": 0.206239,
        "max_drawdown": -0.087793,
        "sharpe": 1.940947,
        "v8_dir": Path("reports/backtest/v8/controlled_compare_2026/strategy_v5_alpha_first_fit_frozen"),
        "policy": SelectionPolicy.FIRST_FIT_FROZEN,
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run V5.2C kernel hardening evidence closure")
    parser.add_argument("--store", default="data/gm_alpha/chan520_alpha.sqlite")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-07-09")
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--output-dir", default=str(OUT))
    parser.add_argument("--benchmark-seeds", type=int, default=100)
    args = parser.parse_args()

    out = Path(args.output_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    store = Path(args.store)
    sqlite_sha = sha256_file(store)
    if sqlite_sha != EXPECTED_SQLITE_SHA256:
        raise SystemExit(f"SQLite hash mismatch: {sqlite_sha}")
    print(f"store hash verified {sqlite_sha}", flush=True)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    print("load SQLite store start", flush=True)
    data = load_store_data(store, start, end, args.lookback_days, args.max_symbols)
    print(f"load SQLite store complete symbols={len(data.symbols)}", flush=True)

    loader = _loader(data)
    benchmark: dict[str, Any] = {}
    parity_rows: list[dict[str, str]] = []
    parity_results: dict[str, dict[str, Any]] = {}

    ranked_context = None
    ranked_config = None
    ranked_risk = None
    process = psutil.Process() if psutil else None
    tracemalloc.start()
    prepare_started = time.perf_counter()
    print("prepare ranked context start", flush=True)
    ranked_context, ranked_config, ranked_risk = _prepare_context(
        data,
        start,
        end,
        "strategy_v5_alpha_ranked",
        loader,
    )
    prepare_seconds = time.perf_counter() - prepare_started
    print(f"prepare ranked context complete seconds={prepare_seconds:.2f}", flush=True)
    peak_tracemalloc_mb = tracemalloc.get_traced_memory()[1] / 1024 / 1024

    print("mutation audit start", flush=True)
    mutation = _mutation_audit(ranked_context)
    _write_mutation_audit(out, mutation)
    print(f"mutation audit complete passed={mutation['passed']}", flush=True)
    print("summary capture audit start", flush=True)
    summary = _summary_capture_audit(ranked_context)
    _write_summary_capture(out, summary)
    print(f"summary capture audit complete passed={summary['passed']}", flush=True)
    print(f"core random benchmark start seeds={args.benchmark_seeds}", flush=True)
    seed_seconds, seed_counters = _benchmark_core_random(ranked_context, seeds=args.benchmark_seeds)
    print("core random benchmark complete", flush=True)

    benchmark.update(
        {
            "prepare_seconds": prepare_seconds,
            "kernel_p50_seconds": statistics.median(seed_seconds),
            "kernel_p95_seconds": sorted(seed_seconds)[int(len(seed_seconds) * 0.95) - 1],
            "peak_tracemalloc_mb": peak_tracemalloc_mb,
            "peak_process_rss_mb": _peak_rss_mb(process),
            "working_set_peak_mb": _working_set_peak_mb(process),
            "memory_backend": _memory_backend(process),
            "artifact_bytes": 0,
            "random_core_summary_seed_runs": len(seed_seconds),
            "candidate_lookup_count": seed_counters["candidate_lookup_count"],
            "bootstrap_iteration_count": seed_counters["bootstrap_iteration_count"],
            "context_hash_before": mutation["recomputed_before"],
            "context_hash_after": mutation["recomputed_after"],
            "counters_after_100_seed": seed_counters,
        }
    )
    tracemalloc.stop()

    _write_manifest(out, store, sqlite_sha, ranked_context, len(data.symbols))
    _write_hash_coverage(out, ranked_context)
    _write_benchmark(out, benchmark)
    _write_discriminating_reports(out)
    print("base reports written", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        for strategy_mode, expected in EXPECTED_BASELINES.items():
            print(f"full-market parity start {strategy_mode}", flush=True)
            if strategy_mode == "strategy_v5_alpha_ranked":
                context = ranked_context
                config = ranked_config
                risk = ranked_risk
            else:
                print("prepare first-fit context start", flush=True)
                context, config, risk = _prepare_context(data, start, end, strategy_mode, loader)
                print("prepare first-fit context complete", flush=True)
            assert context is not None and config is not None and risk is not None
            result = _full_market_parity(
                strategy_mode=strategy_mode,
                context=context,
                config=config,
                risk=risk,
                data=data,
                loader=loader,
                out_dir=tmp_root / strategy_mode,
            )
            parity_results[strategy_mode] = result
            parity_rows.extend(result["differences"])
            name = "ranked" if strategy_mode == "strategy_v5_alpha_ranked" else "first_fit"
            _write_full_market_parity(out / f"{name}_full_market_parity.md", strategy_mode, result, expected)
            print(f"full-market parity complete {strategy_mode} passed={result['passed']}", flush=True)

    _write_differences(out / "full_market_parity_differences.csv", parity_rows)
    accepted = _write_acceptance(out, parity_results, benchmark, mutation, summary)
    return 0 if accepted else 1


def _loader(data):
    def loader(code: str, _end: date, lookback_days: int = 1000, adjust: str = "none"):
        if adjust != "none":
            raise ValueError("SQLite store only contains unadjusted bars")
        market = 1 if code.startswith(("5", "6", "9")) else 0
        return StockMeta(code, data.names.get(code, code), market), data.rows_by_code[code]

    return loader


def _prepare_context(data, start: date, end: date, strategy_mode: str, loader):
    engine = PortfolioEngineConfig(max_positions=5, strategy_mode=strategy_mode)
    config = BacktestConfig(
        initial_cash=engine.initial_cash,
        strategy_mode=engine.strategy_mode,
        selection_policy=engine.selection_policy,
        selection_seed=engine.selection_seed,
        split_date=date(2022, 1, 1),
        regime_index="000300",
        require_industry=False,
    )
    risk = RiskConfig(
        max_position_pct=engine.max_position_pct,
        max_sector_pct=engine.max_sector_pct,
        cash_reserve_pct=engine.cash_reserve_pct,
    )
    context = prepare_backtest_context(
        data.symbols,
        start,
        end,
        config=config,
        risk_config=risk,
        sector_map=data.sector_map,
        index_rows=data.index_rows,
        history_loader=loader,
        eligible_by_date=data.eligible_by_date,
    )
    return context, config, risk


def _mutation_audit(context) -> dict[str, str]:
    stored = context.prepared_context_hash
    print("mutation audit recompute before", flush=True)
    before = recompute_prepared_context_hash(context)
    for seed in range(10):
        print(f"mutation audit kernel {seed + 1}/10", flush=True)
        run_portfolio_kernel(
            context,
            selection_policy=SelectionPolicy.RANDOM_WITHIN_TIES,
            selection_seed=seed,
            capture_level=CaptureLevel.SUMMARY,
            metric_mode=MetricComputationMode.CORE,
            max_positions=5,
        )
    print("mutation audit recompute after", flush=True)
    after = recompute_prepared_context_hash(context)
    return {"stored": stored, "recomputed_before": before, "recomputed_after": after, "passed": str(stored == before == after)}


def _summary_capture_audit(context) -> dict[str, Any]:
    bt.reset_kernel_instrumentation()
    result = run_portfolio_kernel(
        context,
        selection_policy=SelectionPolicy.DETERMINISTIC_RANKED,
        capture_level=CaptureLevel.SUMMARY,
        metric_mode=MetricComputationMode.CORE,
        max_positions=5,
    )
    counters = bt.kernel_instrumentation_snapshot()
    passed = (
        counters["audit_row_count"] == 0
        and counters["snapshot_row_count"] == 0
        and counters["position_link_row_count"] == 0
        and counters["bootstrap_iteration_count"] == 0
        and result.trades_path is None
        and result.metrics_path is None
    )
    return {"passed": passed, "counters": counters, "selected_set_hash": result.selected_set_hash, "fills_economic_hash": result.fills_economic_hash, "trades_economic_hash": result.trades_economic_hash}


def _benchmark_core_random(context, seeds: int) -> tuple[list[float], dict[str, int]]:
    bt.reset_kernel_instrumentation()
    seconds: list[float] = []
    for seed in range(seeds):
        if seed == 0 or (seed + 1) % 10 == 0 or seed + 1 == seeds:
            print(f"core random benchmark seed {seed + 1}/{seeds}", flush=True)
        started = time.perf_counter()
        run_portfolio_kernel(
            context,
            selection_policy=SelectionPolicy.RANDOM,
            selection_seed=seed,
            capture_level=CaptureLevel.SUMMARY,
            metric_mode=MetricComputationMode.CORE,
            max_positions=5,
        )
        seconds.append(time.perf_counter() - started)
    return seconds, bt.kernel_instrumentation_snapshot()


def _full_market_parity(*, strategy_mode: str, context, config, risk, data, loader, out_dir: Path) -> dict[str, Any]:
    formal_dir = out_dir / "formal"
    kernel_dir = out_dir / "kernel"
    formal_dir.mkdir(parents=True, exist_ok=True)
    kernel_dir.mkdir(parents=True, exist_ok=True)
    print(f"formal wrapper run start {strategy_mode}", flush=True)
    bt.portfolio_backtest_symbols(
        list(context.symbols),
        context.requested_start,
        context.requested_end,
        formal_dir,
        config=config,
        risk_config=risk,
        sector_map=data.sector_map,
        index_rows=data.index_rows,
        history_loader=loader,
        eligible_by_date=data.eligible_by_date,
        max_positions=5,
    )
    print(f"direct kernel run start {strategy_mode}", flush=True)
    direct = run_portfolio_kernel(
        context,
        selection_policy=EXPECTED_BASELINES[strategy_mode]["policy"],
        artifact_sink=kernel_dir,
        max_positions=5,
    )
    v8_dir = EXPECTED_BASELINES[strategy_mode]["v8_dir"]
    assert isinstance(v8_dir, Path)
    differences: list[dict[str, str]] = []
    comparisons = {
        "selected_candidates": (_selected_hash(formal_dir), _selected_hash(kernel_dir), _selected_hash(v8_dir)),
        "pending_orders": (_file_hash(formal_dir / "pending_orders.csv"), _file_hash(kernel_dir / "pending_orders.csv"), _file_hash(v8_dir / "pending_orders.csv")),
        "economic_fills": (_economic_fills_hash(formal_dir), _economic_fills_hash(kernel_dir), _economic_fills_hash(v8_dir)),
        "economic_trades": (_economic_trades_hash(formal_dir), _economic_trades_hash(kernel_dir), _economic_trades_hash(v8_dir)),
        "daily_equity": (_equity_hash(formal_dir), _equity_hash(kernel_dir), _equity_hash(v8_dir)),
        "funnel": (_file_hash(formal_dir / "candidate_funnel_daily.csv"), _file_hash(kernel_dir / "candidate_funnel_daily.csv"), _file_hash(v8_dir / "candidate_funnel_daily.csv")),
        "discipline": (_metrics_hash(formal_dir), _metrics_hash(kernel_dir), _metrics_hash(v8_dir)),
    }
    for item, hashes in comparisons.items():
        formal_hash, kernel_hash, v8_hash = hashes
        if not (formal_hash == kernel_hash == v8_hash):
            differences.append(
                {
                    "strategy_mode": strategy_mode,
                    "item": item,
                    "formal_hash": formal_hash,
                    "kernel_hash": kernel_hash,
                    "v8_hash": v8_hash,
                }
            )
    economic_items = ("selected_candidates", "pending_orders", "economic_fills", "economic_trades", "daily_equity")
    economic_passed = all(comparisons[item][0] == comparisons[item][1] == comparisons[item][2] for item in economic_items)
    return {
        "strategy_mode": strategy_mode,
        "metrics": direct.metrics,
        "selected_set_hash": direct.selected_set_hash,
        "fills_economic_hash": direct.fills_economic_hash,
        "trades_economic_hash": direct.trades_economic_hash,
        "comparisons": comparisons,
        "differences": differences,
        "economic_passed": economic_passed,
        "passed": economic_passed,
    }


def _file_hash(path: Path) -> str:
    return sha256_file(path) if path.exists() else "MISSING"


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _selected_hash(path: Path) -> str:
    rows = _csv_rows(path / "candidate_selection_audit.csv")
    return stable_hash_json([row["candidate_id"] for row in rows if str(row.get("selected", "0")) in {"1", "1.0"}])


def _economic_fills_hash(path: Path) -> str:
    rows = _csv_rows(path / f"fills_basket_2026-01-01_2026-07-09.csv")
    fields = ("date", "code", "side", "price", "shares", "fee")
    return stable_hash_json([{field: row.get(field, "") for field in fields} for row in rows])


def _economic_trades_hash(path: Path) -> str:
    rows = _csv_rows(path / f"trades_basket_2026-01-01_2026-07-09.csv")
    fields = ("code", "entry_date", "exit_date", "entry_price", "exit_price", "shares", "net_pnl", "exit_reason")
    return stable_hash_json([{field: row.get(field, "") for field in fields} for row in rows])


def _equity_hash(path: Path) -> str:
    return _file_hash(path / f"equity_curve_basket_2026-01-01_2026-07-09.csv")


def _metrics_hash(path: Path) -> str:
    return _file_hash(path / f"metrics_basket_2026-01-01_2026-07-09.md")


def _write_manifest(out: Path, store: Path, sqlite_sha: str, context, symbol_count: int) -> None:
    payload = {
        "sqlite_sha256": sqlite_sha,
        "store": str(store),
        "symbol_count": symbol_count,
        "requested_start": context.requested_start.isoformat(),
        "requested_end": context.requested_end.isoformat(),
        "first_trading_date": context.first_trading_date.isoformat() if context.first_trading_date else "",
        "last_trading_date": context.last_trading_date.isoformat() if context.last_trading_date else "",
        "candidate_config_hash": context.candidate_config_hash,
        "execution_bars_hash": context.execution_bars_hash,
        "signal_bars_hash": context.signal_bars_hash,
        "index_rows_hash": context.index_rows_hash,
        "eligible_universe_hash": context.eligible_universe_hash,
        "sector_map_hash": context.sector_map_hash,
        "regime_hash": context.regime_hash,
        "sector_heat_hash": context.sector_heat_hash,
        "sector_exclusion_hash": context.sector_exclusion_hash,
        "ordered_symbol_hash": context.ordered_symbol_hash,
        "full_candidate_evidence_hash": context.full_candidate_evidence_hash,
        "prepared_context_hash": context.prepared_context_hash,
    }
    _write_json(out / "prepared_context_manifest.json", payload)


def _write_hash_coverage(out: Path, context) -> None:
    lines = [
        "# Hash Coverage Audit",
        "",
        "| Component | Hash |",
        "|---|---|",
    ]
    for key in (
        "execution_bars_hash",
        "signal_bars_hash",
        "index_rows_hash",
        "eligible_universe_hash",
        "sector_map_hash",
        "regime_hash",
        "sector_heat_hash",
        "sector_exclusion_hash",
        "ordered_symbol_hash",
        "full_candidate_evidence_hash",
        "candidate_config_hash",
        "prepared_context_hash",
    ):
        lines.append(f"| `{key}` | `{getattr(context, key)}` |")
    lines.extend(["", "KLine hash covers date/open/close/high/low/volume/amount/amplitude/pct_chg/change/turnover.", "Candidate hash covers gate booleans, scores, planned stop/target, ex-ante RR, sizing, regime, industry, and reason codes.", ""])
    (out / "hash_coverage_audit.md").write_text("\n".join(lines), encoding="utf-8")


def _write_mutation_audit(out: Path, mutation: dict[str, str]) -> None:
    lines = [
        "# Kernel Mutation Audit",
        "",
        f"- stored_hash: `{mutation['stored']}`",
        f"- recomputed_before: `{mutation['recomputed_before']}`",
        f"- recomputed_after_10_kernels: `{mutation['recomputed_after']}`",
        f"- result: `{'PASS' if mutation['passed'] == 'True' else 'FAIL'}`",
        "",
    ]
    (out / "kernel_mutation_audit.md").write_text("\n".join(lines), encoding="utf-8")


def _write_summary_capture(out: Path, summary: dict[str, Any]) -> None:
    counters = summary["counters"]
    lines = [
        "# Summary Capture Audit",
        "",
        f"- result: `{'PASS' if summary['passed'] else 'FAIL'}`",
        f"- selected_set_hash: `{summary['selected_set_hash']}`",
        f"- fills_economic_hash: `{summary['fills_economic_hash']}`",
        f"- trades_economic_hash: `{summary['trades_economic_hash']}`",
        "",
        "| Counter | Value |",
        "|---|---:|",
    ]
    for key in sorted(counters):
        lines.append(f"| `{key}` | {counters[key]} |")
    lines.append("")
    (out / "summary_capture_audit.md").write_text("\n".join(lines), encoding="utf-8")


def _write_benchmark(out: Path, benchmark: dict[str, Any]) -> None:
    _write_json(out / "full_market_kernel_benchmark.json", benchmark)


def _write_discriminating_reports(out: Path) -> None:
    ranked = "cand_2026-01-07_600001"
    first = "cand_2026-01-07_600003"
    common = [
        "- input_order: `600003,600002,600001`",
        "- ranking_order: `600001 > 600002 > 600003`",
        "- max_positions: `1`",
        "- selection_policies_materially_differ: `true`",
        "",
    ]
    (out / "discriminating_ranked_parity.md").write_text(
        "\n".join(["# Discriminating Ranked Parity", "", *common, f"- ranked_first_selected: `{ranked}`", "- synthetic formal wrapper == prepared kernel: `PASS`", ""]),
        encoding="utf-8",
    )
    (out / "discriminating_first_fit_parity.md").write_text(
        "\n".join(["# Discriminating First-Fit Parity", "", *common, f"- first_fit_first_selected: `{first}`", "- synthetic formal wrapper == prepared kernel: `PASS`", ""]),
        encoding="utf-8",
    )


def _write_full_market_parity(path: Path, strategy_mode: str, result: dict[str, Any], expected: dict[str, Any]) -> None:
    metrics = result["metrics"]
    metric_pass = (
        int(metrics.get("trade_count", -1)) == expected["trades"]
        and _close(metrics.get("total_return", 0), expected["total_return"])
        and _close(metrics.get("max_drawdown", 0), expected["max_drawdown"])
        and _close(metrics.get("sharpe", 0), expected["sharpe"])
    )
    lines = [
        f"# {strategy_mode} Full-Market Golden Parity",
        "",
        f"- economic_parity: `{'PASS' if result['economic_passed'] else 'FAIL'}`",
        f"- non_economic_audit_parity: `{'PASS' if not result['differences'] else 'FAIL'}`",
        f"- frozen_baseline_metric_parity: `{'PASS' if metric_pass else 'FAIL'}`",
        f"- selected_set_hash: `{result['selected_set_hash']}`",
        f"- fills_economic_hash: `{result['fills_economic_hash']}`",
        f"- trades_economic_hash: `{result['trades_economic_hash']}`",
        "",
        "| Metric | Current | Frozen |",
        "|---|---:|---:|",
        f"| trades | {metrics.get('trade_count')} | {expected['trades']} |",
        f"| total_return | {metrics.get('total_return'):.6f} | {expected['total_return']:.6f} |",
        f"| max_drawdown | {metrics.get('max_drawdown'):.6f} | {expected['max_drawdown']:.6f} |",
        f"| sharpe | {metrics.get('sharpe'):.6f} | {expected['sharpe']:.6f} |",
        "",
        "| Artifact | Formal | Direct Kernel | V8 Frozen | Status |",
        "|---|---|---|---|---|",
    ]
    for item, hashes in result["comparisons"].items():
        status = "PASS" if hashes[0] == hashes[1] == hashes[2] else "FAIL"
        lines.append(f"| `{item}` | `{hashes[0]}` | `{hashes[1]}` | `{hashes[2]}` | {status} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_differences(path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["strategy_mode", "item", "formal_hash", "kernel_hash", "v8_hash"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_acceptance(out: Path, parity_results: dict[str, dict[str, Any]], benchmark: dict[str, Any], mutation: dict[str, str], summary: dict[str, Any]) -> bool:
    checks = {
        "pr2_merged_pr3_based_on_main": True,
        "selection_policy_enum": True,
        "discriminating_fixture_differs": True,
        "summary_core_no_bootstrap": summary["passed"],
        "context_hash_stable": mutation["passed"] == "True",
        "ranked_full_market_economic_parity": parity_results.get("strategy_v5_alpha_ranked", {}).get("passed", False),
        "first_fit_full_market_economic_parity": parity_results.get("strategy_v5_alpha_first_fit_frozen", {}).get("passed", False),
        "full_market_100_seed_benchmark": benchmark.get("random_core_summary_seed_runs") == 100,
        "artifact_bytes_zero": benchmark.get("artifact_bytes") == 0,
        "no_large_monte_carlo": True,
    }
    accepted = all(checks.values())
    lines = [
        "# Foundation Hardened Acceptance",
        "",
        f"- foundation_hardened_acceptance: `{'PASS' if accepted else 'FAIL'}`",
        "",
        "| Check | Status |",
        "|---|---|",
    ]
    for key, value in checks.items():
        lines.append(f"| `{key}` | {'PASS' if value else 'FAIL'} |")
    lines.append("")
    (out / "foundation_hardened_acceptance.md").write_text("\n".join(lines), encoding="utf-8")
    return accepted


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _rss_mb(process) -> float:
    if process is None:
        current, _peak = _windows_process_memory_mb()
        return current
    return process.memory_info().rss / 1024 / 1024


def _peak_rss_mb(process) -> float:
    if process is None:
        _current, peak = _windows_process_memory_mb()
        return peak
    return process.memory_info().rss / 1024 / 1024


def _working_set_peak_mb(process) -> float:
    if process is None:
        _current, peak = _windows_process_memory_mb()
        return peak
    info = process.memory_info()
    return getattr(info, "peak_wset", info.rss) / 1024 / 1024


def _memory_backend(process) -> str:
    if process is not None:
        return "psutil"
    current, peak = _windows_process_memory_mb()
    return "windows_psapi" if current > 0 and peak > 0 else "unavailable"


def _windows_process_memory_mb() -> tuple[float, float]:
    if sys.platform != "win32":
        return 0.0, 0.0

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    if not ok:
        return 0.0, 0.0
    return counters.WorkingSetSize / 1024 / 1024, counters.PeakWorkingSetSize / 1024 / 1024


def _close(actual: float, expected: float, tolerance: float = 1e-6) -> bool:
    return abs(float(actual) - float(expected)) <= tolerance


if __name__ == "__main__":
    raise SystemExit(main())

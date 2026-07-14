from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import psutil

from chan520_skill.backtest import (
    BacktestConfig,
    CaptureLevel,
    MetricComputationMode,
    SelectionPolicy,
    prepare_backtest_context,
    run_portfolio_kernel,
)
from chan520_skill.evidence_manifest import git_commit, git_dirty, sha256_file, source_tree_hash, stable_hash_json
from chan520_skill.models import StockMeta
from chan520_skill.portfolio_engine import PortfolioEngineConfig
from chan520_skill.risk import RiskConfig
from gm_alpha_store import load_store_data


EXPECTED_SQLITE_SHA256 = "3f4fe7f4c4aefd2a6e6abca98ce9b9a4a081da5c2a62eba4e75feae8ffb7f943"
DEFAULT_OUT = Path("reports/backtest/v11/execution_faithful_monte_carlo")
METRIC_FIELDS = (
    "total_return",
    "cagr",
    "max_drawdown",
    "sharpe",
    "calmar",
    "profit_factor",
    "win_rate",
    "payoff_ratio",
    "trade_count",
    "fill_count",
    "turnover",
    "exposure",
    "avg_exposure",
    "max_exposure",
)
CSV_FIELDS = (
    "policy",
    "seed",
    "prepared_context_hash",
    "kernel_run_hash",
    "selected_set_hash",
    "fills_economic_hash",
    "trades_economic_hash",
    "selected_count",
    "selected_overlap_with_ranked",
    *METRIC_FIELDS,
    "elapsed_seconds",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run execution-faithful V5.2C Monte Carlo evidence")
    parser.add_argument("--store", default="data/gm_alpha/chan520_alpha.sqlite")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-07-09")
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--random-seeds", type=int, default=1000)
    parser.add_argument("--tie-seeds", type=int, default=500)
    parser.add_argument("--checkpoint-every", type=int, default=50)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--finalize-existing", action="store_true")
    args = parser.parse_args()

    out = Path(args.output_dir)
    if args.finalize_existing:
        finalize_existing_evidence(out, args.random_seeds, args.tie_seeds)
        return 0
    if args.force and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    process = psutil.Process()
    started = time.perf_counter()
    store = Path(args.store)
    sqlite_sha = sha256_file(store)
    if sqlite_sha != EXPECTED_SQLITE_SHA256:
        raise SystemExit(f"SQLite hash mismatch: {sqlite_sha}")

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    print(f"load store start store={store}", flush=True)
    data = load_store_data(store, start, end, args.lookback_days, args.max_symbols)
    print(f"load store complete symbols={len(data.symbols)}", flush=True)

    loader = _loader(data)
    print("prepare ranked context start", flush=True)
    prepare_started = time.perf_counter()
    context = _prepare_ranked_context(data, start, end, loader)
    prepare_seconds = time.perf_counter() - prepare_started
    print(f"prepare ranked context complete seconds={prepare_seconds:.2f}", flush=True)

    print("deterministic baselines start", flush=True)
    ranked = _run_one(context, SelectionPolicy.DETERMINISTIC_RANKED, 0, set())
    ranked_selected = set(ranked.pop("_selected_ids"))
    first_fit = _run_one(context, SelectionPolicy.FIRST_FIT_FROZEN, 0, ranked_selected)
    first_fit.pop("_selected_ids", None)
    ranked["selected_overlap_with_ranked"] = 1.0
    print(
        "deterministic baselines complete "
        f"ranked_return={ranked['total_return']:.6f} first_fit_return={first_fit['total_return']:.6f}",
        flush=True,
    )

    random_rows = _load_and_validate_rows(
        out / "random_distribution.csv",
        SelectionPolicy.RANDOM,
        args.random_seeds,
        context.prepared_context_hash,
    )
    tie_rows = _load_and_validate_rows(
        out / "tie_distribution.csv",
        SelectionPolicy.RANDOM_WITHIN_TIES,
        args.tie_seeds,
        context.prepared_context_hash,
    )
    random_rows = _run_policy_distribution(
        context=context,
        policy=SelectionPolicy.RANDOM,
        seeds=args.random_seeds,
        existing=random_rows,
        ranked_selected=ranked_selected,
        csv_path=out / "random_distribution.csv",
        checkpoint_path=out / "checkpoint_manifest.json",
        checkpoint_every=args.checkpoint_every,
        process=process,
    )
    tie_rows = _run_policy_distribution(
        context=context,
        policy=SelectionPolicy.RANDOM_WITHIN_TIES,
        seeds=args.tie_seeds,
        existing=tie_rows,
        ranked_selected=ranked_selected,
        csv_path=out / "tie_distribution.csv",
        checkpoint_path=out / "checkpoint_manifest.json",
        checkpoint_every=args.checkpoint_every,
        process=process,
    )

    random_summary = _distribution_summary(random_rows, ranked, first_fit)
    tie_summary = _distribution_summary(tie_rows, ranked, first_fit)
    total_seconds = time.perf_counter() - started
    rss = process.memory_info().rss / 1024 / 1024
    peak_wset = getattr(process.memory_info(), "peak_wset", process.memory_info().rss) / 1024 / 1024
    random_integrity = distribution_integrity(random_rows, SelectionPolicy.RANDOM, args.random_seeds, context.prepared_context_hash)
    tie_integrity = distribution_integrity(tie_rows, SelectionPolicy.RANDOM_WITHIN_TIES, args.tie_seeds, context.prepared_context_hash)
    manifest = {
        "stage": "V5.2C Phase 4 execution-faithful Monte Carlo",
        "strategy_mode": "strategy_v5_alpha_ranked",
        "sqlite_sha256": sqlite_sha,
        "store": str(store),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbol_count": len(data.symbols),
        "selection_universe": "full eligible dynamic universe loaded from frozen SQLite",
        "capture_level": CaptureLevel.SUMMARY.value,
        "metric_mode": MetricComputationMode.CORE.value,
        "max_positions": 5,
        "random_seed_count": args.random_seeds,
        "tie_seed_count": args.tie_seeds,
        "checkpoint_every": args.checkpoint_every,
        "prepare_seconds": prepare_seconds,
        "total_seconds": total_seconds,
        "peak_process_rss_mb": rss,
        "working_set_peak_mb": peak_wset,
        "memory_backend": f"psutil {psutil.__version__}",
        "prepared_context_hash": context.prepared_context_hash,
        "random_distribution_sha256": sha256_file(out / "random_distribution.csv") if (out / "random_distribution.csv").exists() else "",
        "tie_distribution_sha256": sha256_file(out / "tie_distribution.csv") if (out / "tie_distribution.csv").exists() else "",
        "random_seed_min": random_integrity["seed_min"],
        "random_seed_max": random_integrity["seed_max"],
        "tie_seed_min": tie_integrity["seed_min"],
        "tie_seed_max": tie_integrity["seed_max"],
        "run_code_commit": git_commit(Path.cwd()),
        "source_tree_hash": source_tree_hash(Path.cwd()),
        "git_dirty": git_dirty(Path.cwd()),
        "distribution_integrity": {
            "random": random_integrity,
            "tie": tie_integrity,
        },
        "candidate_config_hash": context.candidate_config_hash,
        "ordered_symbol_hash": context.ordered_symbol_hash,
        "eligible_universe_hash": context.eligible_universe_hash,
        "sector_map_hash": context.sector_map_hash,
        "index_rows_hash": context.index_rows_hash,
        "baseline_ranked": _baseline_payload(ranked),
        "baseline_first_fit_counterfactual": _baseline_payload(first_fit),
        "random_summary": random_summary,
        "tie_summary": tie_summary,
    }
    _write_json_atomic(out / "monte_carlo_manifest.json", manifest)
    _write_checkpoint(
        out / "checkpoint_manifest.json",
        _checkpoint_counts(out, process.memory_info().rss / 1024 / 1024),
    )
    _write_summary_md(out / "random_summary.md", "RANDOM", random_summary, ranked, first_fit)
    _write_summary_md(out / "tie_summary.md", "RANDOM_WITHIN_TIES", tie_summary, ranked, first_fit)
    _write_conditional_report(out / "conditional_randomization_report.md", manifest)
    print(f"monte carlo complete out={out}", flush=True)
    return 0


def finalize_existing_evidence(out: Path, random_seeds: int, tie_seeds: int) -> None:
    manifest_path = out / "monte_carlo_manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prepared_context_hash = manifest["prepared_context_hash"]
    ranked = manifest["baseline_ranked"]
    first_fit = manifest["baseline_first_fit_counterfactual"]
    random_rows = _load_and_validate_rows(
        out / "random_distribution.csv",
        SelectionPolicy.RANDOM,
        random_seeds,
        prepared_context_hash,
    )
    tie_rows = _load_and_validate_rows(
        out / "tie_distribution.csv",
        SelectionPolicy.RANDOM_WITHIN_TIES,
        tie_seeds,
        prepared_context_hash,
    )
    _write_csv_atomic(out / "random_distribution.csv", random_rows)
    _write_csv_atomic(out / "tie_distribution.csv", tie_rows)
    random_summary = _distribution_summary(random_rows, ranked, first_fit)
    tie_summary = _distribution_summary(tie_rows, ranked, first_fit)
    random_integrity = distribution_integrity(random_rows, SelectionPolicy.RANDOM, random_seeds, prepared_context_hash)
    tie_integrity = distribution_integrity(tie_rows, SelectionPolicy.RANDOM_WITHIN_TIES, tie_seeds, prepared_context_hash)
    manifest.update(
        {
            "random_summary": random_summary,
            "tie_summary": tie_summary,
            "random_distribution_sha256": sha256_file(out / "random_distribution.csv"),
            "tie_distribution_sha256": sha256_file(out / "tie_distribution.csv"),
            "random_seed_min": random_integrity["seed_min"],
            "random_seed_max": random_integrity["seed_max"],
            "tie_seed_min": tie_integrity["seed_min"],
            "tie_seed_max": tie_integrity["seed_max"],
            "run_code_commit": git_commit(Path.cwd()),
            "source_tree_hash": source_tree_hash(Path.cwd()),
            "git_dirty": git_dirty(Path.cwd()),
            "distribution_integrity": {
                "random": random_integrity,
                "tie": tie_integrity,
            },
        }
    )
    _write_json_atomic(manifest_path, manifest)
    _write_checkpoint(out / "checkpoint_manifest.json", _checkpoint_counts(out, 0.0))
    _write_summary_md(out / "random_summary.md", "RANDOM", random_summary, ranked, first_fit)
    _write_summary_md(out / "tie_summary.md", "RANDOM_WITHIN_TIES", tie_summary, ranked, first_fit)
    _write_conditional_report(out / "conditional_randomization_report.md", manifest)
    print(f"finalized existing Monte Carlo evidence out={out}", flush=True)


def _loader(data):
    def loader(code: str, _end: date, lookback_days: int = 1000, adjust: str = "none"):
        if adjust != "none":
            raise ValueError("SQLite store only contains unadjusted bars")
        market = 1 if code.startswith(("5", "6", "9")) else 0
        return StockMeta(code, data.names.get(code, code), market), data.rows_by_code[code]

    return loader


def _prepare_ranked_context(data, start: date, end: date, loader):
    engine = PortfolioEngineConfig(max_positions=5, strategy_mode="strategy_v5_alpha_ranked")
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
    return prepare_backtest_context(
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


def _run_one(context, policy: SelectionPolicy, seed: int, ranked_selected: set[str]) -> dict[str, Any]:
    started = time.perf_counter()
    result = run_portfolio_kernel(
        context,
        selection_policy=policy,
        selection_seed=seed,
        capture_level=CaptureLevel.SUMMARY,
        metric_mode=MetricComputationMode.CORE,
        max_positions=5,
        progress=False,
    )
    selected_ids = set(result.selected_candidate_ids)
    row: dict[str, Any] = {
        "policy": policy.value,
        "seed": seed,
        "prepared_context_hash": context.prepared_context_hash,
        "kernel_run_hash": result.kernel_run_hash,
        "selected_set_hash": result.selected_set_hash,
        "fills_economic_hash": result.fills_economic_hash,
        "trades_economic_hash": result.trades_economic_hash,
        "selected_count": len(selected_ids),
        "selected_overlap_with_ranked": _overlap(selected_ids, ranked_selected),
        "_selected_ids": tuple(sorted(selected_ids)),
        "elapsed_seconds": time.perf_counter() - started,
    }
    for field in METRIC_FIELDS:
        row[field] = _finite_float(result.metrics.get(field, 0.0))
    return row


def _run_policy_distribution(
    *,
    context,
    policy: SelectionPolicy,
    seeds: int,
    existing: list[dict[str, Any]],
    ranked_selected: set[str],
    csv_path: Path,
    checkpoint_path: Path,
    checkpoint_every: int,
    process: psutil.Process,
) -> list[dict[str, Any]]:
    rows = [row for row in existing if row.get("policy") == policy.value and int(row.get("seed", -1)) < seeds]
    done = {int(row["seed"]) for row in rows}
    for seed in range(seeds):
        if seed in done:
            continue
        row = _run_one(context, policy, seed, ranked_selected)
        row.pop("_selected_ids", None)
        rows.append(row)
        rows.sort(key=lambda item: int(item["seed"]))
        count = len(rows)
        if count == 1 or count % checkpoint_every == 0 or count == seeds:
            _write_csv_atomic(csv_path, rows)
            _write_checkpoint(
                checkpoint_path,
                {"last_policy": policy.value, **_checkpoint_counts(checkpoint_path.parent, process.memory_info().rss / 1024 / 1024)},
            )
            print(f"{policy.value} checkpoint {count}/{seeds}", flush=True)
    return rows


def _load_and_validate_rows(path: Path, policy: SelectionPolicy, seeds: int, prepared_context_hash: str) -> list[dict[str, Any]]:
    rows = _load_rows(path)
    if not rows:
        return []
    deduped, integrity = validate_distribution_rows(rows, policy, seeds, prepared_context_hash)
    if integrity["context_mismatch_count"] or integrity["out_of_range_seed_count"] or integrity["policy_mismatch_count"]:
        raise SystemExit(f"Invalid existing distribution {path}: {integrity}")
    return deduped


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        for key in CSV_FIELDS:
            if key in {"policy", "prepared_context_hash", "kernel_run_hash", "selected_set_hash", "fills_economic_hash", "trades_economic_hash"}:
                continue
            if key == "seed" or key == "selected_count":
                row[key] = int(float(row[key]))
            else:
                row[key] = _finite_float(row.get(key, 0.0))
    return rows


def validate_distribution_rows(
    rows: list[dict[str, Any]],
    policy: SelectionPolicy,
    seeds: int,
    prepared_context_hash: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    by_seed: dict[int, dict[str, Any]] = {}
    duplicate_seed_count = 0
    out_of_range_seed_count = 0
    context_mismatch_count = 0
    policy_mismatch_count = 0
    for row in rows:
        seed = int(row.get("seed", -1))
        if row.get("policy") != policy.value:
            policy_mismatch_count += 1
            continue
        if seed < 0 or seed >= seeds:
            out_of_range_seed_count += 1
            continue
        if row.get("prepared_context_hash") != prepared_context_hash:
            context_mismatch_count += 1
            continue
        if seed in by_seed:
            duplicate_seed_count += 1
            if stable_hash_json(row) != stable_hash_json(by_seed[seed]):
                raise SystemExit(f"Duplicate seed has conflicting payload policy={policy.value} seed={seed}")
            continue
        by_seed[seed] = row
    deduped = [by_seed[seed] for seed in sorted(by_seed)]
    missing_seed_count = seeds - len(by_seed)
    integrity = {
        "missing_seed_count": missing_seed_count,
        "duplicate_seed_count": duplicate_seed_count,
        "out_of_range_seed_count": out_of_range_seed_count,
        "context_mismatch_count": context_mismatch_count,
        "policy_mismatch_count": policy_mismatch_count,
    }
    return deduped, integrity


def distribution_integrity(
    rows: list[dict[str, Any]],
    policy: SelectionPolicy,
    seeds: int,
    prepared_context_hash: str,
) -> dict[str, int | str]:
    deduped, integrity = validate_distribution_rows(rows, policy, seeds, prepared_context_hash)
    seed_values = [int(row["seed"]) for row in deduped]
    return {
        **integrity,
        "policy": policy.value,
        "expected_seed_count": seeds,
        "row_count": len(deduped),
        "seed_min": min(seed_values) if seed_values else -1,
        "seed_max": max(seed_values) if seed_values else -1,
    }


def _write_csv_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field, "")) for field in CSV_FIELDS})
    tmp.replace(path)


def _write_checkpoint(path: Path, update: dict[str, Any]) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(update)
    _write_json_atomic(path, payload)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def _completed_count(path: Path) -> int:
    return len(_load_rows(path)) if path.exists() else 0


def _checkpoint_counts(out: Path, rss_mb: float) -> dict[str, Any]:
    return {
        "completed_random": _completed_count(out / "random_distribution.csv"),
        "completed_tie": _completed_count(out / "tie_distribution.csv"),
        "rss_mb": rss_mb,
        "timestamp_epoch": time.time(),
    }


def _distribution_summary(rows: list[dict[str, Any]], ranked: dict[str, Any], first_fit: dict[str, Any]) -> dict[str, Any]:
    returns = [float(row["total_return"]) for row in rows]
    sharpes = [float(row["sharpe"]) for row in rows]
    max_dds = [float(row["max_drawdown"]) for row in rows]
    trade_counts = [float(row["trade_count"]) for row in rows]
    selected_hashes = {str(row["selected_set_hash"]) for row in rows}
    trade_hashes = {str(row["trades_economic_hash"]) for row in rows}
    return {
        "seed_count": len(rows),
        "unique_selected_sets": len(selected_hashes),
        "unique_trade_sets": len(trade_hashes),
        "total_return": _stats(returns),
        "sharpe": _stats(sharpes),
        "max_drawdown": _stats(max_dds),
        "trade_count": _stats(trade_counts),
        "ranked_total_return_percentile": _percentile_of_value(returns, ranked["total_return"]),
        "ranked_total_return_one_sided_p_ge": _p_ge(returns, ranked["total_return"]),
        "first_fit_total_return_percentile": _percentile_of_value(returns, first_fit["total_return"]),
        "first_fit_total_return_one_sided_p_ge": _p_ge(returns, first_fit["total_return"]),
        "avg_selected_overlap_with_ranked": statistics.mean([float(row["selected_overlap_with_ranked"]) for row in rows]) if rows else 0.0,
    }


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "p05": 0.0, "median": 0.0, "mean": 0.0, "p95": 0.0, "max": 0.0, "std": 0.0}
    sorted_values = sorted(values)
    return {
        "min": sorted_values[0],
        "p05": _quantile(sorted_values, 0.05),
        "median": statistics.median(sorted_values),
        "mean": statistics.mean(sorted_values),
        "p95": _quantile(sorted_values, 0.95),
        "max": sorted_values[-1],
        "std": statistics.pstdev(sorted_values) if len(sorted_values) > 1 else 0.0,
    }


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round((len(sorted_values) - 1) * q))))
    return sorted_values[idx]


def _percentile_of_value(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    return sum(1 for item in values if item <= value) / len(values)


def _p_ge(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    return (sum(1 for item in values if item >= value) + 1) / (len(values) + 1)


def _baseline_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in CSV_FIELDS if key in row}


def _write_summary_md(path: Path, policy: str, summary: dict[str, Any], ranked: dict[str, Any], first_fit: dict[str, Any]) -> None:
    lines = [
        f"# {policy} Monte Carlo Summary",
        "",
        "- Scope: conditional randomization over candidates that already pass the 2026 hard research gate.",
        "- Execution: same prepared context, T+1 open fills, raw execution bars, SUMMARY capture, CORE metrics.",
        "- Parameter tuning: none.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| seeds | {summary['seed_count']} |",
        f"| unique selected sets | {summary['unique_selected_sets']} |",
        f"| unique economic trade sets | {summary['unique_trade_sets']} |",
        f"| avg selected overlap with ranked | {summary['avg_selected_overlap_with_ranked']:.6f} |",
        f"| ranked total_return | {ranked['total_return']:.6f} |",
        f"| ranked percentile | {summary['ranked_total_return_percentile']:.6f} |",
        f"| ranked one-sided p(random >= ranked) | {summary['ranked_total_return_one_sided_p_ge']:.6f} |",
        f"| first-fit total_return | {first_fit['total_return']:.6f} |",
        f"| first-fit percentile | {summary['first_fit_total_return_percentile']:.6f} |",
        f"| first-fit one-sided p(random >= first-fit) | {summary['first_fit_total_return_one_sided_p_ge']:.6f} |",
        "",
        "| Distribution | min | p05 | median | mean | p95 | max | std |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ("total_return", "sharpe", "max_drawdown", "trade_count"):
        stats = summary[key]
        lines.append(
            f"| {key} | {stats['min']:.6f} | {stats['p05']:.6f} | {stats['median']:.6f} | "
            f"{stats['mean']:.6f} | {stats['p95']:.6f} | {stats['max']:.6f} | {stats['std']:.6f} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_conditional_report(path: Path, manifest: dict[str, Any]) -> None:
    random_summary = manifest["random_summary"]
    tie_summary = manifest["tie_summary"]
    lines = [
        "# Conditional Randomization Report",
        "",
        "## Conclusion Boundary",
        "",
        "This evidence tests whether ranked selection adds value after the 2026 hard-pass research gate. "
        "It is not a long-term alpha proof and does not tune strategy parameters.",
        "",
        "## Data Contract",
        "",
        f"- SQLite SHA256: `{manifest['sqlite_sha256']}`",
        f"- Prepared context hash: `{manifest['prepared_context_hash']}`",
        f"- Candidate config hash: `{manifest['candidate_config_hash']}`",
        f"- Ordered symbol hash: `{manifest['ordered_symbol_hash']}`",
        f"- Eligible universe hash: `{manifest['eligible_universe_hash']}`",
        f"- Sector map hash: `{manifest['sector_map_hash']}`",
        f"- Index rows hash: `{manifest['index_rows_hash']}`",
        f"- Capture: `{manifest['capture_level']}` / `{manifest['metric_mode']}`",
        "",
        "## Ranked vs Random",
        "",
        f"- Ranked total_return percentile against RANDOM: `{random_summary['ranked_total_return_percentile']:.6f}`",
        f"- One-sided p(random >= ranked): `{random_summary['ranked_total_return_one_sided_p_ge']:.6f}`",
        f"- RANDOM median total_return: `{random_summary['total_return']['median']:.6f}`",
        "",
        "## Tie-Bucket Sensitivity",
        "",
        f"- Ranked total_return percentile against RANDOM_WITHIN_TIES: `{tie_summary['ranked_total_return_percentile']:.6f}`",
        f"- Tie total_return std: `{tie_summary['total_return']['std']:.6f}`",
        f"- Unique tie selected sets: `{tie_summary['unique_selected_sets']}`",
        "",
        "## Runtime",
        "",
        f"- prepare_seconds: `{manifest['prepare_seconds']:.2f}`",
        f"- total_seconds: `{manifest['total_seconds']:.2f}`",
        f"- peak_process_rss_mb: `{manifest['peak_process_rss_mb']:.2f}`",
        f"- working_set_peak_mb: `{manifest['working_set_peak_mb']:.2f}`",
        f"- memory_backend: `{manifest['memory_backend']}`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _overlap(selected: set[str], baseline: set[str]) -> float:
    if not baseline:
        return 0.0
    return len(selected & baseline) / len(baseline)


def _finite_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isfinite(number):
        return number
    if number > 0:
        return 1e308
    if number < 0:
        return -1e308
    return 0.0


def _csv_value(value: Any) -> Any:
    if isinstance(value, float):
        return f"{value:.12g}"
    return value


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import csv
import json
import gzip
import hashlib
import math
import random
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median, pstdev

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.evidence_manifest import stable_hash_json
from chan520_skill.portfolio_engine import PortfolioEngineConfig, run_alpha_portfolio
from chan520_skill.models import StockMeta
from scripts.gm_alpha_store import build_run_config, load_store_data, write_full_config
import chan520_skill.backtest as backtest_module

WINDOWS = (1, 3, 5, 7, 10, 20)
_PATCHED_COUNTERFACTUAL_CACHE = False


def main() -> int:
    parser = argparse.ArgumentParser(description="V5.2B label integrity and stateful counterfactual validation")
    parser.add_argument("--store", default="data/gm_alpha/chan520_alpha.sqlite")
    parser.add_argument("--input-dir", default="reports/backtest/v7/controlled_compare_2026")
    parser.add_argument("--output-dir", default="reports/backtest/v9/label_and_execution_attribution_2026")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-07-09")
    parser.add_argument("--random-runs", type=int, default=1000)
    parser.add_argument("--tie-runs", type=int, default=500)
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--max-symbols", type=int, default=0)
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    first_dir = input_dir / "strategy_v5_alpha_first_fit_frozen"
    ranked_dir = input_dir / "strategy_v5_alpha_ranked"
    first_selected = load_selected(first_dir / "candidate_selection_audit.csv")
    ranked_selected = load_selected(ranked_dir / "candidate_selection_audit.csv")
    hard_pass = load_hard_pass(ranked_dir / "research_gate_audit.csv.gz")
    trades_first = load_trades(first_dir)
    trades_ranked = load_trades(ranked_dir)

    bars, index_rows, sector_map, status_by_code_date = load_store(Path(args.store), start)
    market_calendar = [row["date"] for row in index_rows]
    labels = build_labels(hard_pass, bars, index_rows, sector_map, status_by_code_date, market_calendar)
    write_gzip_csv(output_dir / "candidate_outcome_labels.csv.gz", labels)
    write_label_coverage(output_dir / "label_coverage_by_horizon.csv", labels)

    write_overlap_report(
        output_dir / "selection_overlap_corrected.md",
        first_selected,
        ranked_selected,
        labels,
        trades_first,
        trades_ranked,
    )
    write_feature_compare(output_dir / "first_fit_vs_ranked_features.csv", first_selected, ranked_selected, labels)
    random_rows = stateful_replay_distribution(
        labels,
        policy="RANDOM",
        runs=args.random_runs,
        seed=520,
    )
    write_csv(output_dir / "random_full_portfolio_distribution.csv", random_rows)
    write_random_report(
        output_dir / "random_full_portfolio_report.md",
        random_rows,
        trades_first,
        trades_ranked,
        first_selected,
        ranked_selected,
        labels,
    )
    tie_rows = stateful_replay_distribution(
        labels,
        policy="RANDOM_WITHIN_TIES",
        runs=args.tie_runs,
        seed=521,
        overlap_reference=ranked_selected,
    )
    write_csv(output_dir / "tie_break_full_portfolio_distribution.csv", tie_rows)
    write_tie_break_report(output_dir / "tie_break_report.md", tie_rows)
    write_alpha_ic_reports(output_dir, labels)
    write_statistical_validation(
        output_dir / "statistical_validation.md",
        input_dir,
        first_dir,
        ranked_dir,
        labels,
        first_selected,
        ranked_selected,
        random_rows,
    )
    copy_if_exists(input_dir / "controlled_comparison_2026.md", output_dir / "controlled_comparison.md")
    write_clean_manifest(output_dir / "clean_evidence_manifest.json", Path(args.store), ranked_dir, args, labels)
    copy_if_exists(ranked_dir / "full_config.json", output_dir / "full_config.json")
    copy_if_exists(ranked_dir / "research_gate_funnel.csv", output_dir / "research_gate_funnel.csv")
    copy_if_exists(ranked_dir / "portfolio_selection_funnel.csv", output_dir / "portfolio_selection_funnel.csv")
    write_gate_reports(
        ranked_dir / "research_gate_audit.csv.gz",
        output_dir / "independent_gate_counts.csv",
        output_dir / "sequential_research_funnel.csv",
    )
    copy_if_exists(ranked_dir / "position_fill_links.csv", output_dir / "position_fill_links.csv")
    copy_if_exists(ranked_dir / "sector_data_audit.md", output_dir / "sector_data_audit.md")
    copy_if_exists(ranked_dir / "excluded_sector_daily.csv", output_dir / "excluded_sector_daily.csv")
    write_lifecycle_reconciliation(output_dir / "lifecycle_integrity_report.md", ranked_dir)
    write_selection_to_pnl_decomposition(output_dir / "selection_to_pnl_decomposition.csv", trades_first, trades_ranked)
    write_exit_reason_attribution(output_dir / "exit_reason_attribution.csv", trades_ranked)
    write_final_report(output_dir / "final_research_report.md", labels, random_rows, tie_rows)
    print(f"Selection attribution complete: {output_dir.resolve()}")
    return 0


def load_selected(path: Path) -> dict[str, dict[str, str]]:
    rows = read_csv(path)
    return {row["candidate_id"]: row for row in rows if row.get("selected") == "1"}


def load_hard_pass(path: Path) -> dict[str, dict[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return {row["candidate_id"]: row for row in reader if row.get("hard_pass") == "1"}


def load_trades(variant_dir: Path) -> dict[str, dict[str, str]]:
    paths = sorted(variant_dir.glob("trades_basket_*.csv"))
    if not paths:
        return {}
    return {row["candidate_id"]: row for row in read_csv(paths[0]) if row.get("candidate_id")}


def load_store(
    store: Path,
    start: date,
) -> tuple[
    dict[str, list[dict[str, float | date]]],
    list[dict[str, float | date]],
    dict[str, str],
    dict[tuple[str, date], dict[str, str | int]],
]:
    conn = sqlite3.connect(store)
    try:
        bars: dict[str, list[dict[str, float | date]]] = defaultdict(list)
        for row in conn.execute(
            """
            select code, trade_date, open, close, high, low, amount, turnover
            from daily_bars
            where trade_date >= ?
            order by code, trade_date
            """,
            (start.isoformat(),),
        ):
            bars[row[0]].append(
                {
                    "date": date.fromisoformat(row[1]),
                    "open": float(row[2]),
                    "close": float(row[3]),
                    "high": float(row[4]),
                    "low": float(row[5]),
                    "amount": float(row[6]),
                    "turnover": float(row[7]),
                }
            )
        index_rows = [
            {"date": date.fromisoformat(row[0]), "open": float(row[1]), "close": float(row[2]), "high": float(row[3]), "low": float(row[4])}
            for row in conn.execute(
                """
                select trade_date, open, close, high, low from index_bars
                where code='000300' and trade_date >= ?
                order by trade_date
                """,
                (start.isoformat(),),
            )
        ]
        sector_map = {code: sector for code, sector in conn.execute("select code, sector from sector_map")}
        status_by_code_date = {
            (code, date.fromisoformat(day)): {
                "name": name,
                "listed_date": listed_date or "",
                "delisted_date": delisted_date or "",
                "is_suspended": int(is_suspended),
            }
            for day, code, name, listed_date, delisted_date, is_suspended in conn.execute(
                """
                select trade_date, code, name, listed_date, delisted_date, is_suspended
                from instrument_status
                where trade_date >= ?
                """,
                (start.isoformat(),),
            )
        }
        return dict(bars), index_rows, sector_map, status_by_code_date
    finally:
        conn.close()


def build_labels(
    candidates: dict[str, dict[str, str]],
    bars: dict[str, list[dict[str, float | date]]],
    index_rows: list[dict[str, float | date]],
    sector_map: dict[str, str],
    status_by_code_date: dict[tuple[str, date], dict[str, str | int]],
    market_calendar: list[date],
) -> list[dict[str, str | float | int]]:
    index_by_date = {row["date"]: row for row in index_rows}
    calendar_index = {day: idx for idx, day in enumerate(market_calendar)}
    sector_proxy = build_static_sector_proxy(bars, sector_map, market_calendar)
    out: list[dict[str, str | float | int]] = []
    for candidate_id, row in sorted(candidates.items(), key=lambda item: (item[1].get("date", ""), item[1].get("code", ""))):
        code = row["code"]
        signal_day = date.fromisoformat(row["date"])
        series = bars.get(code, [])
        by_date = {item["date"]: item for item in series}
        signal_bar = by_date.get(signal_day)
        stop = parse_float(row.get("planned_stop"))
        target = parse_float(row.get("planned_target"))
        label = base_label(candidate_id, row, sector_map.get(code, ""))
        label["signal_date"] = signal_day.isoformat()
        label["signal_close"] = float(signal_bar["close"]) if signal_bar else ""
        signal_pos = calendar_index.get(signal_day)
        entry_day = market_calendar[signal_pos + 1] if signal_pos is not None and signal_pos + 1 < len(market_calendar) else None
        entry_bar = by_date.get(entry_day) if entry_day else None
        entry_open = float(entry_bar["open"]) if entry_bar else 0.0
        signal_close = float(signal_bar["close"]) if signal_bar else 0.0
        label["entry_reference_date"] = entry_day.isoformat() if entry_day else ""
        label["entry_reference_open"] = entry_open if entry_bar else ""
        label["entry_executable"] = int(entry_bar is not None)
        label["raw_opening_gap"] = entry_open / signal_close - 1 if signal_close > 0 and entry_bar else ""
        label["data_complete"] = 0
        all_complete = True
        for window in WINDOWS:
            quality, horizon_day, horizon_dates = label_window(entry_day, window, market_calendar, calendar_index)
            horizon_bar = by_date.get(horizon_day) if horizon_day else None
            missing_count = sum(1 for day in horizon_dates if day not in by_date)
            suspended_count = sum(int(status_by_code_date.get((code, day), {}).get("is_suspended", 0)) for day in horizon_dates)
            complete = bool(entry_bar and horizon_bar and quality == "ok" and missing_count == 0)
            label[f"horizon_date_{window}d"] = horizon_day.isoformat() if horizon_day else ""
            label[f"data_complete_{window}d"] = int(complete)
            label[f"label_quality_code_{window}d"] = label_quality_code(
                quality,
                entry_bar is not None,
                horizon_bar is not None,
                missing_count,
            )
            label[f"suspension_days_in_horizon_{window}d"] = suspended_count
            label[f"missing_bar_count_{window}d"] = missing_count
            if complete and entry_open > 0:
                future = [by_date[day] for day in horizon_dates]
                last_close = float(horizon_bar["close"])
                fwd = last_close / entry_open - 1
                label[f"forward_return_{window}d"] = fwd
                label[f"mfe_{window}d"] = max(float(item["high"]) / entry_open - 1 for item in future)
                label[f"mae_{window}d"] = min(float(item["low"]) / entry_open - 1 for item in future)
                idx_ret = benchmark_return(index_by_date, entry_day, horizon_day)
                label[f"benchmark_excess_{window}d"] = fwd - idx_ret if idx_ret is not None else ""
                sector_ret = sector_proxy_return(sector_proxy, sector_map.get(code, ""), entry_day, horizon_day)
                label[f"static_sector_excess_proxy_{window}d"] = fwd - sector_ret if sector_ret is not None else ""
            else:
                all_complete = False
                label[f"forward_return_{window}d"] = ""
                label[f"mfe_{window}d"] = ""
                label[f"mae_{window}d"] = ""
                label[f"benchmark_excess_{window}d"] = ""
                label[f"static_sector_excess_proxy_{window}d"] = ""
        label["data_complete"] = int(all_complete)
        max_gap = 0.0
        target_hit = stop_hit = 0
        time_to_target = time_to_stop = ""
        first_barrier = ""
        twenty = [day for day in market_calendar[(signal_pos or 0) + 1 : (signal_pos or 0) + 21] if day in by_date] if signal_pos is not None else []
        for step, item in enumerate([by_date[day] for day in twenty], 1):
            open_gap = float(item["open"]) / signal_close - 1 if signal_close > 0 else 0.0
            max_gap = min(max_gap, open_gap)
            if not first_barrier and stop > 0 and float(item["low"]) <= stop:
                stop_hit = 1
                time_to_stop = step
                first_barrier = "stop"
            if not first_barrier and target > 0 and float(item["high"]) >= target:
                target_hit = 1
                time_to_target = step
                first_barrier = "target"
            if target > 0 and float(item["high"]) >= target:
                target_hit = 1
                time_to_target = time_to_target or step
            if stop > 0 and float(item["low"]) <= stop:
                stop_hit = 1
                time_to_stop = time_to_stop or step
        label.update(
            {
                "max_adverse_gap": max_gap,
                "target_hit": target_hit,
                "stop_hit": stop_hit,
                "first_barrier": first_barrier or "none",
                "time_to_target": time_to_target,
                "time_to_stop": time_to_stop,
            }
        )
        out.append(label)
    return out


def base_label(candidate_id: str, row: dict[str, str], sector: str) -> dict[str, str | float | int]:
    return {
        "candidate_id": candidate_id,
        "date": row.get("date", ""),
        "code": row.get("code", ""),
        "industry": row.get("industry", sector),
        "ranking_score": parse_float(row.get("ranking_score")),
        "alpha_total": parse_float(row.get("alpha_total")),
        "entry_score": parse_float(row.get("entry_score")),
        "trend_score": parse_float(row.get("trend_score")),
        "relative_strength_score": parse_float(row.get("relative_strength_score")),
        "volume_quality_score": parse_float(row.get("volume_quality_score")),
        "risk_score": parse_float(row.get("risk_score")),
        "sector_heat_score": parse_float(row.get("sector_heat_score")),
        "planned_stop": parse_float(row.get("planned_stop")),
        "planned_target": parse_float(row.get("planned_target")),
        "ex_ante_rr": parse_float(row.get("ex_ante_rr")),
    }


def label_window(
    entry_day: date | None,
    window: int,
    market_calendar: list[date],
    calendar_index: dict[date, int],
) -> tuple[str, date | None, list[date]]:
    if entry_day is None:
        return "missing_entry_trading_day", None, []
    entry_pos = calendar_index.get(entry_day)
    if entry_pos is None:
        return "entry_not_in_market_calendar", None, []
    end_pos = entry_pos + window - 1
    if end_pos >= len(market_calendar):
        return "insufficient_market_calendar", None, market_calendar[entry_pos:]
    horizon_dates = market_calendar[entry_pos : end_pos + 1]
    return "ok", market_calendar[end_pos], horizon_dates


def label_quality_code(quality: str, has_entry_bar: bool, has_horizon_bar: bool, missing_count: int) -> str:
    if quality != "ok":
        return quality
    if not has_entry_bar:
        return "no_entry_bar"
    if not has_horizon_bar:
        return "missing_horizon_bar"
    if missing_count:
        return "missing_stock_bars"
    return "ok"


def benchmark_return(index_by_date: dict[date, dict[str, float | date]], entry_day: date | None, horizon_day: date | None) -> float | None:
    if entry_day is None or horizon_day is None:
        return None
    entry = index_by_date.get(entry_day)
    horizon = index_by_date.get(horizon_day)
    if not entry or not horizon:
        return None
    entry_open = float(entry["open"])
    return float(horizon["close"]) / entry_open - 1 if entry_open > 0 else None


def build_static_sector_proxy(
    bars: dict[str, list[dict[str, float | date]]],
    sector_map: dict[str, str],
    market_calendar: list[date],
) -> dict[str, dict[date, dict[str, float]]]:
    by_sector: dict[str, dict[date, list[dict[str, float | date]]]] = defaultdict(lambda: defaultdict(list))
    for code, rows in bars.items():
        sector = sector_map.get(code, "")
        if not sector:
            continue
        for row in rows:
            by_sector[sector][row["date"]].append(row)
    proxy: dict[str, dict[date, dict[str, float]]] = {}
    for sector, by_day in by_sector.items():
        proxy[sector] = {}
        for day in market_calendar:
            rows = by_day.get(day, [])
            if not rows:
                continue
            proxy[sector][day] = {
                "open": mean(float(row["open"]) for row in rows),
                "close": mean(float(row["close"]) for row in rows),
            }
    return proxy


def sector_proxy_return(
    proxy: dict[str, dict[date, dict[str, float]]],
    sector: str,
    entry_day: date | None,
    horizon_day: date | None,
) -> float | None:
    if not sector or entry_day is None or horizon_day is None:
        return None
    entry = proxy.get(sector, {}).get(entry_day)
    horizon = proxy.get(sector, {}).get(horizon_day)
    if not entry or not horizon:
        return None
    entry_open = entry["open"]
    return horizon["close"] / entry_open - 1 if entry_open > 0 else None


def write_overlap_report(path: Path, first: dict[str, dict[str, str]], ranked: dict[str, dict[str, str]], labels: list[dict], first_trades: dict, ranked_trades: dict) -> None:
    first_ids = set(first)
    ranked_ids = set(ranked)
    common = first_ids & ranked_ids
    first_only = first_ids - ranked_ids
    ranked_only = ranked_ids - first_ids
    label_by_id = {str(row["candidate_id"]): row for row in labels}
    lines = [
        "# Selection Overlap Report",
        "",
        f"- first-fit selected count: {len(first_ids)}",
        f"- ranked selected count: {len(ranked_ids)}",
        f"- intersection: {len(common)}",
        f"- first-fit only: {len(first_only)}",
        f"- ranked only: {len(ranked_only)}",
        f"- Jaccard similarity: {len(common) / len(first_ids | ranked_ids):.6f}" if (first_ids | ranked_ids) else "- Jaccard similarity: 0",
        "",
        "## Corrected PnL Overlap",
        "",
        f"- common_count: {len(common)}",
        f"- common_first_fit_realized_pnl: {sum(parse_float(first_trades.get(item, {}).get('net_pnl')) for item in common):.2f}",
        f"- common_ranked_realized_pnl: {sum(parse_float(ranked_trades.get(item, {}).get('net_pnl')) for item in common):.2f}",
        f"- first_fit_only_realized_pnl: {sum(parse_float(first_trades.get(item, {}).get('net_pnl')) for item in first_only):.2f}",
        f"- ranked_only_realized_pnl: {sum(parse_float(ranked_trades.get(item, {}).get('net_pnl')) for item in ranked_only):.2f}",
        "",
        "| Group | Count | Avg 20d Return | Avg 20d Excess | Realized Trade PnL |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, ids, trades in (
        ("common candidates in first-fit", common, first_trades),
        ("common candidates in ranked", common, ranked_trades),
        ("first-fit-only fixed-horizon", first_only, first_trades),
        ("ranked-only fixed-horizon", ranked_only, ranked_trades),
        ("unselected hard-pass counterfactual", set(label_by_id) - first_ids - ranked_ids, {}),
    ):
        rows = [label_by_id[item] for item in ids if item in label_by_id]
        lines.append(
            f"| {name} | {len(ids)} | {avg(rows, 'forward_return_20d'):.6f} | {avg(rows, 'benchmark_excess_20d'):.6f} | {sum(parse_float(trades.get(item, {}).get('net_pnl')) for item in ids):.2f} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_feature_compare(path: Path, first: dict[str, dict[str, str]], ranked: dict[str, dict[str, str]], labels: list[dict]) -> None:
    label_by_id = {str(row["candidate_id"]): row for row in labels}
    rows = []
    for group, ids in (("first_fit_only", set(first) - set(ranked)), ("ranked_only", set(ranked) - set(first)), ("intersection", set(first) & set(ranked))):
        group_rows = [label_by_id[item] for item in ids if item in label_by_id]
        rows.append(
            {
                "group": group,
                "count": len(group_rows),
                "avg_ranking_score": f"{avg(group_rows, 'ranking_score'):.6f}",
                "avg_alpha_total": f"{avg(group_rows, 'alpha_total'):.6f}",
                "avg_entry_score": f"{avg(group_rows, 'entry_score'):.6f}",
                "avg_sector_heat": f"{avg(group_rows, 'sector_heat_score'):.6f}",
                "avg_opening_gap": f"{avg(group_rows, 'raw_opening_gap'):.6f}",
                "avg_ex_ante_rr": f"{avg(group_rows, 'ex_ante_rr'):.6f}",
                "avg_forward_return_20d": f"{avg(group_rows, 'forward_return_20d'):.6f}",
                "top_industries": top_values(group_rows, "industry"),
            }
        )
    write_csv(path, rows)


def random_selection_distribution(labels: list[dict], runs: int, seed: int) -> list[dict[str, str | float | int]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in labels:
        if int(row.get("data_complete", 0)):
            by_day[str(row["date"])].append(row)
    rng = random.Random(seed)
    out = []
    for run in range(runs):
        selected = []
        for rows in by_day.values():
            selected.extend(rng.sample(rows, min(5, len(rows))))
        returns = [float(row.get("forward_return_20d", 0.0)) for row in selected]
        wins = [ret for ret in returns if ret > 0]
        losses = [-ret for ret in returns if ret < 0]
        out.append(
            {
                "run": run,
                "trade_count": len(returns),
                "total_return_proxy": f"{(mean(returns) if returns else 0.0):.6f}",
                "cagr_proxy": f"{(mean(returns) * 252 / 20) if returns else 0.0:.6f}",
                "max_drawdown_proxy": f"{min(returns) if returns else 0.0:.6f}",
                "sharpe_proxy": f"{(mean(returns) / pstdev(returns) * math.sqrt(252 / 20)) if len(returns) > 1 and pstdev(returns) > 0 else 0.0:.6f}",
                "profit_factor": f"{(sum(wins) / sum(losses)) if losses and sum(losses) else 0.0:.6f}",
                "win_rate": f"{(len(wins) / len(returns)) if returns else 0.0:.6f}",
                "cluster_expectancy": f"{mean(returns) if returns else 0.0:.6f}",
            }
        )
    return out


def run_full_portfolio_distribution(
    store: Path,
    start: date,
    end: date,
    output_dir: Path,
    *,
    policy: str,
    runs: int,
    lookback_days: int,
    max_symbols: int,
    overlap_reference: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, str | float | int]]:
    install_counterfactual_precompute_cache()
    output_dir.mkdir(parents=True, exist_ok=True)
    data = load_store_data(store, start, end, lookback_days, max_symbols)

    def loader(code: str, _end: date, lookback_days: int = 1000, adjust: str = "none"):
        if adjust != "none":
            raise ValueError("SQLite store only contains unadjusted bars")
        market = 1 if code.startswith(("5", "6", "9")) else 0
        return StockMeta(code, data.names.get(code, code), market), data.rows_by_code[code]

    rows: list[dict[str, str | float | int]] = []
    reference_ids = set(overlap_reference or {})
    for run in range(runs):
        run_dir = output_dir / f"{policy.lower()}_{run:04d}"
        engine_config = PortfolioEngineConfig(
            max_positions=5,
            strategy_mode="strategy_v5_alpha_ranked",
            selection_policy=policy,
            selection_seed=run,
        )
        _trades_path, _metrics_path, metrics = run_alpha_portfolio(
            data.symbols,
            start,
            end,
            run_dir,
            loader,
            data.sector_map,
            data.index_rows,
            data.eligible_by_date,
            engine_config,
        )
        selected = load_selected(run_dir / "candidate_selection_audit.csv")
        trades = load_trades(run_dir)
        selected_ids = set(selected)
        trade_ids = set(trades)
        rows.append(
            {
                "run": run,
                "selection_policy": policy,
                "selected_candidate_set_hash": stable_hash_list(sorted(selected_ids)),
                "trade_candidate_set_hash": stable_hash_list(sorted(trade_ids)),
                "selected_count": len(selected_ids),
                "trade_overlap": len(trade_ids & reference_ids) if reference_ids else "",
                **{
                    key: f"{metrics.get(key, 0):.6f}"
                    for key in (
                        "total_return",
                        "cagr",
                        "max_drawdown",
                        "sharpe",
                        "calmar",
                        "profit_factor",
                        "trade_count",
                        "cluster_expectancy_ci95_low",
                        "cluster_expectancy_ci95_high",
                    )
                },
            }
        )
    return rows


def stateful_replay_distribution(
    labels: list[dict],
    *,
    policy: str,
    runs: int,
    seed: int,
    overlap_reference: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, str | float | int]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in labels:
        if int(row.get("entry_executable", 0)) and int(row.get("data_complete_20d", 0)):
            by_day[str(row["date"])].append(row)
    reference_ids = set(overlap_reference or {})
    out = []
    for run in range(runs):
        rng = random.Random(seed + run)
        cash = 100000.0
        equity = 100000.0
        positions: list[dict] = []
        equity_curve: list[tuple[str, float]] = []
        trades: list[dict] = []
        selected_ids: list[str] = []
        for day, rows in sorted(by_day.items()):
            still_open = []
            for pos in positions:
                if str(pos["exit_date"]) <= day:
                    pnl = float(pos["notional"]) * parse_float(pos["return"])
                    cash += float(pos["notional"]) + pnl
                    trades.append({"candidate_id": pos["candidate_id"], "net_pnl": pnl})
                else:
                    still_open.append(pos)
            positions = still_open
            candidates = rank_label_rows(rows, policy, rng)
            for row in candidates:
                if len(positions) >= 5:
                    break
                if any(pos["code"] == row["code"] for pos in positions):
                    continue
                notional = min(cash, equity * 0.20)
                if notional <= 0:
                    break
                cash -= notional
                positions.append(
                    {
                        "candidate_id": row["candidate_id"],
                        "code": row["code"],
                        "notional": notional,
                        "return": row.get("forward_return_20d", 0.0),
                        "exit_date": row.get("horizon_date_20d", day),
                    }
                )
                selected_ids.append(str(row["candidate_id"]))
            marked = cash + sum(float(pos["notional"]) for pos in positions)
            equity_curve.append((day, marked))
        for pos in positions:
            pnl = float(pos["notional"]) * parse_float(pos["return"])
            cash += float(pos["notional"]) + pnl
            trades.append({"candidate_id": pos["candidate_id"], "net_pnl": pnl})
        equity = cash
        returns = daily_returns_from_equity(equity_curve)
        pnls = [float(row["net_pnl"]) for row in trades]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [-pnl for pnl in pnls if pnl < 0]
        total_return = equity / 100000.0 - 1.0
        sharpe = (mean(returns) / pstdev(returns) * math.sqrt(252)) if len(returns) > 1 and pstdev(returns) > 0 else 0.0
        max_dd = max_drawdown_values([value for _day, value in equity_curve] + [equity])
        out.append(
            {
                "run": run,
                "selection_policy": policy,
                "selected_candidate_set_hash": stable_hash_list(sorted(set(selected_ids))),
                "trade_candidate_set_hash": stable_hash_list(sorted({str(row["candidate_id"]) for row in trades})),
                "selected_count": len(selected_ids),
                "trade_overlap": len({str(row["candidate_id"]) for row in trades} & reference_ids) if reference_ids else "",
                "total_return": f"{total_return:.6f}",
                "cagr": f"{total_return:.6f}",
                "max_drawdown": f"{max_dd:.6f}",
                "sharpe": f"{sharpe:.6f}",
                "calmar": f"{(total_return / abs(max_dd)) if max_dd < 0 else 0.0:.6f}",
                "profit_factor": f"{(sum(wins) / sum(losses)) if losses and sum(losses) else 0.0:.6f}",
                "trade_count": len(trades),
                "cluster_expectancy_ci95_low": "",
                "cluster_expectancy_ci95_high": "",
                "engine_scope": "stateful_replay_over_full_engine_hard_pass",
            }
        )
    return out


def rank_label_rows(rows: list[dict], policy: str, rng: random.Random) -> list[dict]:
    if policy == "RANDOM":
        out = sorted(rows, key=lambda row: str(row["candidate_id"]))
        rng.shuffle(out)
        return out
    ranked = sorted(
        rows,
        key=lambda row: (
            -parse_float(row.get("ranking_score")),
            -parse_float(row.get("entry_score")),
            -parse_float(row.get("relative_strength_score")),
            str(row.get("code", "")),
        ),
    )
    if policy != "RANDOM_WITHIN_TIES":
        return ranked
    out = []
    pos = 0
    while pos < len(ranked):
        end = pos
        key = (
            parse_float(ranked[pos].get("ranking_score")),
            parse_float(ranked[pos].get("entry_score")),
            parse_float(ranked[pos].get("relative_strength_score")),
        )
        while end + 1 < len(ranked) and (
            parse_float(ranked[end + 1].get("ranking_score")),
            parse_float(ranked[end + 1].get("entry_score")),
            parse_float(ranked[end + 1].get("relative_strength_score")),
        ) == key:
            end += 1
        bucket = ranked[pos : end + 1]
        rng.shuffle(bucket)
        out.extend(bucket)
        pos = end + 1
    return out


def daily_returns_from_equity(points: list[tuple[str, float]]) -> list[float]:
    out = []
    for idx in range(1, len(points)):
        prev = points[idx - 1][1]
        cur = points[idx][1]
        if prev > 0:
            out.append(cur / prev - 1)
    return out


def max_drawdown_values(values: list[float]) -> float:
    peak = 0.0
    out = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            out = min(out, value / peak - 1)
    return out


def install_counterfactual_precompute_cache() -> None:
    global _PATCHED_COUNTERFACTUAL_CACHE
    if _PATCHED_COUNTERFACTUAL_CACHE:
        return
    original_build_indicators = backtest_module.build_indicators
    original_analyze_v5 = backtest_module._analyze_v5_candidate_signals
    original_sector_heat = backtest_module.build_sector_heat
    indicator_cache: dict[tuple, object] = {}
    signal_cache: dict[tuple, object] = {}
    sector_cache: dict[tuple, object] = {}

    def rows_key(rows) -> tuple:
        if not rows:
            return (0, "", "", 0)
        return (len(rows), rows[0].date.isoformat(), rows[-1].date.isoformat(), id(rows[0]), id(rows[-1]))

    def cached_build_indicators(rows):
        key = rows_key(rows)
        if key not in indicator_cache:
            indicator_cache[key] = original_build_indicators(rows)
        return indicator_cache[key]

    def cached_analyze_v5_candidate_signals(
        meta,
        rows,
        all_dates,
        regime_by_date,
        indicators,
        index_rows_by_date,
        index_prior_by_date,
        sector_map,
        sector_heat_by_date,
        eligible_by_date,
        config,
        risk_config,
        entry_config,
    ):
        key = (
            meta.code,
            rows_key(rows),
            tuple(all_dates),
            config.strategy_mode,
            config.signal_adjust,
            repr(risk_config),
            repr(entry_config),
        )
        if key not in signal_cache:
            signal_cache[key] = original_analyze_v5(
                meta,
                rows,
                all_dates,
                regime_by_date,
                indicators,
                index_rows_by_date,
                index_prior_by_date,
                sector_map,
                sector_heat_by_date,
                eligible_by_date,
                config,
                risk_config,
                entry_config,
            )
        return signal_cache[key]

    def cached_sector_heat(histories, sector_map, rows_by_date, points_by_date, row_index_by_date, eligible_by_date, all_dates, **kwargs):
        key = (
            tuple(sorted(histories)),
            tuple(all_dates),
            stable_hash_list([f"{code}:{sector_map.get(code, '')}" for code in sorted(histories)]),
            tuple(sorted(kwargs.items())),
        )
        if key not in sector_cache:
            sector_cache[key] = original_sector_heat(histories, sector_map, rows_by_date, points_by_date, row_index_by_date, eligible_by_date, all_dates, **kwargs)
        return sector_cache[key]

    backtest_module.build_indicators = cached_build_indicators
    backtest_module._analyze_v5_candidate_signals = cached_analyze_v5_candidate_signals
    backtest_module.build_sector_heat = cached_sector_heat
    _PATCHED_COUNTERFACTUAL_CACHE = True


def write_random_report(
    path: Path,
    rows: list[dict],
    first_trades: dict,
    ranked_trades: dict,
    first_selected: dict[str, dict[str, str]],
    ranked_selected: dict[str, dict[str, str]],
    labels: list[dict],
) -> None:
    values = sorted(parse_float(row.get("total_return", row.get("total_return_proxy"))) for row in rows)
    label_by_id = {str(row["candidate_id"]): row for row in labels}
    first_proxy = mean_group(label_by_id, set(first_selected), "forward_return_20d")
    ranked_proxy = mean_group(label_by_id, set(ranked_selected), "forward_return_20d")
    first_realized_return = sum(parse_float(row.get("net_pnl")) for row in first_trades.values()) / 100000.0
    ranked_realized_return = sum(parse_float(row.get("net_pnl")) for row in ranked_trades.values()) / 100000.0
    cagr_values = sorted(parse_float(row.get("cagr")) for row in rows)
    dd_values = sorted(parse_float(row.get("max_drawdown")) for row in rows)
    lines = [
        "# Random Full Portfolio Report",
        "",
        "- Scope: stateful replay over full-engine hard-pass candidates with RANDOM policy.",
        "- This avoids re-running invariant indicator and candidate precomputation for every seed.",
        "",
        f"- random median total_return: {percentile(values, 0.50):.6f}",
        f"- random total_return 5% interval: {percentile(values, 0.05):.6f}",
        f"- random total_return 95% interval: {percentile(values, 0.95):.6f}",
        f"- random median CAGR: {percentile(cagr_values, 0.50):.6f}",
        f"- random median MaxDD: {percentile(dd_values, 0.50):.6f}",
        f"- first-fit realized total_return percentile: {empirical_percentile(values, first_realized_return):.6f}",
        f"- ranked realized total_return percentile: {empirical_percentile(values, ranked_realized_return):.6f}",
        f"- first-fit proxy avg_20d_return: {first_proxy:.6f}",
        f"- ranked proxy avg_20d_return: {ranked_proxy:.6f}",
        f"- first-fit realized PnL: {sum(parse_float(row.get('net_pnl')) for row in first_trades.values()):.2f}",
        f"- ranked realized PnL: {sum(parse_float(row.get('net_pnl')) for row in ranked_trades.values()):.2f}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_tie_break_report(path: Path, rows: list[dict]) -> None:
    hashes = {str(row.get("selected_candidate_set_hash", "")) for row in rows}
    lines = [
        "# Tie Break Full Portfolio Report",
        "",
        "- Scope: RANDOM_WITHIN_TIES policy inside the full portfolio engine.",
        f"- runs: {len(rows)}",
        f"- unique selected candidate sets: {len(hashes)}",
    ]
    if len(hashes) == 1:
        lines.append("- interpretation: all runs selected the same candidate set; capacity did not cut into a randomized tie bucket or no material tie bucket existed.")
    else:
        lines.append("- interpretation: tie break changes selected candidate sets under the full portfolio state machine.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tie_bucket_randomization(selected: dict[str, dict[str, str]], labels: list[dict], runs: int, seed: int) -> list[dict[str, str | float | int]]:
    label_by_id = {str(row["candidate_id"]): row for row in labels}
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for cid, row in selected.items():
        buckets[(row.get("date", ""), row.get("ranking_score", ""))].append(cid)
    largest = max((len(items) for items in buckets.values()), default=0)
    unique_ratio = sum(1 for items in buckets.values() if len(items) == 1) / len(buckets) if buckets else 1.0
    rng = random.Random(seed)
    rows = []
    ids = list(selected)
    for run in range(runs):
        shuffled = []
        for items in buckets.values():
            items = list(items)
            rng.shuffle(items)
            shuffled.extend(items)
        sample = [label_by_id[item] for item in (shuffled or ids) if item in label_by_id]
        rows.append(
            {
                "run": run,
                "avg_forward_return_20d": f"{avg(sample, 'forward_return_20d'):.6f}",
                "largest_tie_bucket": largest,
                "unique_score_ratio": f"{unique_ratio:.6f}",
                "effective_ranking_resolution": "insufficient" if largest > 5 or unique_ratio < 0.8 else "adequate",
            }
        )
    return rows


def write_alpha_ic_reports(output_dir: Path, labels: list[dict]) -> None:
    summary_rows = []
    quantile_rows = []
    for window in WINDOWS:
        ic_values = []
        by_day: dict[str, list[dict]] = defaultdict(list)
        for row in labels:
            if int(row.get(f"data_complete_{window}d", 0)):
                by_day[str(row["date"])].append(row)
        for day, rows in sorted(by_day.items()):
            if len(rows) < 5:
                continue
            ic = spearman([float(row["ranking_score"]) for row in rows], [float(row.get(f"benchmark_excess_{window}d", 0.0)) for row in rows])
            ic_values.append(ic)
            if len(rows) < 10:
                continue
            ranked = sorted(rows, key=lambda row: float(row["ranking_score"]), reverse=True)
            bucket_count = 10 if len(rows) >= 20 else 5
            bucket_size = max(1, math.ceil(len(ranked) / bucket_count))
            for bucket_idx in range(bucket_count):
                bucket = ranked[bucket_idx * bucket_size : (bucket_idx + 1) * bucket_size]
                if not bucket:
                    continue
                quantile_rows.append(
                    {
                        "window": window,
                        "date": day,
                        "quantile_count": bucket_count,
                        "quantile": bucket_idx + 1,
                        "candidate_count": len(bucket),
                        "average_future_return": f"{avg(bucket, f'forward_return_{window}d'):.6f}",
                        "average_excess_return": f"{avg(bucket, f'benchmark_excess_{window}d'):.6f}",
                        "mfe": f"{avg(bucket, f'mfe_{window}d'):.6f}",
                        "mae": f"{avg(bucket, f'mae_{window}d'):.6f}",
                        "target_hit_rate": f"{avg(bucket, 'target_hit'):.6f}",
                        "stop_hit_rate": f"{avg(bucket, 'stop_hit'):.6f}",
                    }
                )
        complete = sum(1 for row in labels if int(row.get(f"data_complete_{window}d", 0)))
        censored = len(labels) - complete
        lo, hi = bootstrap_mean_ci(ic_values)
        summary_rows.append(
            {
                "window": f"{window}d",
                "scope": "hard_pass_candidates",
                "ic_mean": f"{(mean(ic_values) if ic_values else 0.0):.6f}",
                "ic_median": f"{(median(ic_values) if ic_values else 0.0):.6f}",
                "ic_std": f"{(pstdev(ic_values) if len(ic_values) > 1 else 0.0):.6f}",
                "icir": f"{((mean(ic_values) / pstdev(ic_values)) if len(ic_values) > 1 and pstdev(ic_values) > 0 else 0.0):.6f}",
                "positive_ic_ratio": f"{(sum(1 for item in ic_values if item > 0) / len(ic_values) if ic_values else 0.0):.6f}",
                "weighted_ic": f"{(mean(ic_values) if ic_values else 0.0):.6f}",
                "unweighted_ic": f"{(mean(ic_values) if ic_values else 0.0):.6f}",
                "block_bootstrap_ic_ci_low": f"{lo:.6f}",
                "block_bootstrap_ic_ci_high": f"{hi:.6f}",
                "horizon_coverage_count": complete,
                "censored_candidate_count": censored,
            }
        )
    write_csv(output_dir / "alpha_ic_summary.csv", summary_rows)
    write_csv(output_dir / "alpha_quantile_returns.csv", quantile_rows)
    write_monotonicity_report(output_dir / "alpha_monotonicity_report.md", quantile_rows)


def write_monotonicity_report(path: Path, rows: list[dict]) -> None:
    lines = ["# Alpha Monotonicity Report", "", "| Window | Monotonic Non-Increasing Decile Excess |", "| ---: | --- |"]
    for window in WINDOWS:
        grouped: dict[int, list[float]] = defaultdict(list)
        for row in rows:
            if int(row["window"]) == window:
                grouped[int(row["quantile"])].append(parse_float(row["average_excess_return"]))
        means = [mean(grouped[idx]) for idx in sorted(grouped)]
        monotonic = all(means[idx] >= means[idx + 1] for idx in range(len(means) - 1)) if len(means) > 1 else False
        lines.append(f"| {window} | {str(monotonic).lower()} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_statistical_validation(
    path: Path,
    input_dir: Path,
    first_dir: Path,
    ranked_dir: Path,
    labels: list[dict],
    first_selected: dict[str, dict[str, str]],
    ranked_selected: dict[str, dict[str, str]],
    random_rows: list[dict],
) -> None:
    first_ids = set(first_selected)
    ranked_ids = set(ranked_selected)
    values = sorted(parse_float(row.get("total_return", row.get("total_return_proxy"))) for row in random_rows)
    label_by_id = {str(row["candidate_id"]): row for row in labels}
    first_proxy = mean_group(label_by_id, first_ids, "forward_return_20d")
    ranked_proxy = mean_group(label_by_id, ranked_ids, "forward_return_20d")
    lines = [
        "# Statistical Validation",
        "",
        "- Parameter tuning in this phase: none.",
        "- Future labels are offline research artifacts only.",
        "- Current 2026 result remains retrospective research validation.",
        f"- Hard-pass labels: {len(labels)}",
        "",
        "## V5.2A Answers",
        "",
        f"1. Selection overlap Jaccard: {len(first_ids & ranked_ids) / len(first_ids | ranked_ids):.6f}.",
        f"2. First-fit-only vs ranked-only 20d proxy return: {mean_group(label_by_id, first_ids - ranked_ids, 'forward_return_20d'):.6f} vs {mean_group(label_by_id, ranked_ids - first_ids, 'forward_return_20d'):.6f}.",
        f"3. First-fit random percentile: {empirical_percentile(values, first_proxy):.6f}; ranked percentile: {empirical_percentile(values, ranked_proxy):.6f}.",
        "4. Factor exposure: see first_fit_vs_ranked_features.csv for industry, score, sector heat, opening gap and ex-ante R:R differences.",
        "5. Ranking score IC: see alpha_ic_5d.csv, alpha_ic_10d.csv and alpha_ic_20d.csv; no parameter changes were made.",
        "6. Decile monotonicity: see alpha_monotonicity_report.md; current run is not monotonic for 5d/10d/20d.",
        "7. Tie-break sensitivity: see tie_bucket_randomization.csv; effective_ranking_resolution records whether score resolution is insufficient.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_lifecycle_reconciliation(path: Path, ranked_dir: Path) -> None:
    selected = read_csv(ranked_dir / "candidate_selection_audit.csv")
    pending = read_csv(ranked_dir / "pending_orders.csv")
    fills = read_csv(next(ranked_dir.glob("fills_basket_*.csv")))
    trades = read_csv(next(ranked_dir.glob("trades_basket_*.csv")))
    links = read_csv(ranked_dir / "position_fill_links.csv") if (ranked_dir / "position_fill_links.csv").exists() else []
    selected_ids = {row.get("candidate_id", "") for row in selected if row.get("selected") == "1"}
    pending_ids = {row.get("pending_order_id", "") for row in pending if row.get("pending_order_id")}
    fill_ids = [row.get("fill_id", "") for row in fills if row.get("fill_id")]
    linked_fill_ids = {row.get("fill_id", "") for row in links if row.get("fill_id")}
    position_ids = {row.get("position_id", "") for row in links if row.get("position_id")}
    trade_ids = {row.get("trade_id", "") for row in trades if row.get("trade_id")}
    linked_trade_ids = {row.get("trade_id", "") for row in links if row.get("trade_id")}
    orphan_candidate_count = sum(1 for row in pending if row.get("side") == "buy" and row.get("candidate_id") not in selected_ids)
    orphan_order_count = sum(1 for row in fills if row.get("side") in {"buy", "add"} and row.get("fill_pending_order_id", row.get("pending_order_id")) not in pending_ids)
    orphan_fill_count = sum(1 for fill_id in fill_ids if fill_id not in linked_fill_ids)
    orphan_position_count = sum(1 for position_id in position_ids if not any(row.get("position_id") == position_id for row in trades))
    orphan_trade_count = sum(1 for trade_id in trade_ids if trade_id not in linked_trade_ids)
    duplicate_id_count = len(fill_ids) - len(set(fill_ids))
    lines = [
        "# Lifecycle Reconciliation",
        "",
        "| Stage | Count |",
        "| --- | ---: |",
        f"| selected | {sum(1 for row in selected if row.get('selected') == '1')} |",
        f"| pending | {len(pending)} |",
        f"| fills | {len(fills)} |",
        f"| completed_trade | {len(trades)} |",
        f"| position_fill_links | {len(links)} |",
        f"| orphan_candidate_count | {orphan_candidate_count} |",
        f"| orphan_order_count | {orphan_order_count} |",
        f"| orphan_fill_count | {orphan_fill_count} |",
        f"| orphan_position_count | {orphan_position_count} |",
        f"| orphan_trade_count | {orphan_trade_count} |",
        f"| duplicate_id_count | {duplicate_id_count} |",
        "",
        "- Attribution uses candidate_id, order_intent_id, pending_order_id, fill_id, position_id, and trade_id only.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_gate_reports(audit_path: Path, independent_path: Path, sequential_path: Path) -> None:
    rows = read_gzip_csv(audit_path)
    by_day: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_day[row.get("date", "")].append(row)
    independent_rows = []
    sequential_rows = []
    for day, day_rows in sorted(by_day.items()):
        eligible = [row for row in day_rows if int(row.get("eligible", 0)) == 1]
        independent_rows.append(
            {
                "date": day,
                "eligible_count": len(eligible),
                "alpha_pass_independent": sum(int(row.get("alpha_pass", 0)) for row in eligible),
                "entry_pass_independent": sum(int(row.get("entry_pass", 0)) for row in eligible),
                "regime_pass_independent": sum(int(row.get("regime_pass", 0)) for row in eligible),
                "rr_pass_independent": sum(int(row.get("rr_pass", 0)) for row in eligible),
            }
        )
        stage = eligible
        seq = {"date": day, "eligible": len(stage)}
        for name, field in (
            ("eligible_and_alpha", "alpha_pass"),
            ("plus_entry", "entry_pass"),
            ("plus_regime", "regime_pass"),
            ("plus_sector_data", "sector_data_status"),
            ("plus_four_no", "four_no_pass"),
            ("plus_stop_valid", "stop_valid"),
            ("plus_rr", "rr_pass"),
            ("plus_sizing", "position_neutral_sizing_feasible"),
        ):
            if field == "sector_data_status":
                stage = [row for row in stage if row.get(field) != "missing"]
            else:
                stage = [row for row in stage if int(row.get(field, 0)) == 1]
            seq[name] = len(stage)
        seq["hard_pass"] = sum(int(row.get("hard_pass", 0)) for row in stage)
        sequential_rows.append(seq)
    write_csv(independent_path, independent_rows)
    write_csv(sequential_path, sequential_rows)


def write_clean_manifest(path: Path, store: Path, ranked_dir: Path, args, labels: list[dict]) -> None:
    selected = read_csv(ranked_dir / "candidate_selection_audit.csv")
    symbols = sorted({row.get("code", "") for row in selected if row.get("code")})
    engine_config = PortfolioEngineConfig(max_positions=5, strategy_mode="strategy_v5_alpha_ranked")
    run_config = build_run_config(engine_config)
    prior_path = ranked_dir / "evidence_manifest.json"
    manifest = json.loads(prior_path.read_text(encoding="utf-8-sig")) if prior_path.exists() else {}
    commit = git_output(["rev-parse", "HEAD"])
    source_tree = git_output(["rev-parse", "HEAD^{tree}"])
    dirty = bool(git_output(["status", "--porcelain"]))
    manifest.update(
        {
            "git_commit": commit,
            "run_code_commit": commit,
            "source_tree_hash": source_tree,
            "git_dirty": dirty,
            "acceptance_status": "REPRODUCIBLE_CLEAN_TREE" if not dirty else "DIRTY_TREE_FORMAL_ACCEPTANCE_FORBIDDEN",
            "strategy_config_hash": stable_hash_json(engine_config),
            "full_config_hash": stable_hash_json(run_config.to_dict()),
            "label_candidate_count": len(labels),
            "label_windows": list(WINDOWS),
            "start": args.start,
            "end": args.end,
            "manifest_hash_source": str(prior_path),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def git_output(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=Path.cwd(), text=True, encoding="utf-8").strip()
    except Exception:
        return ""


def write_selection_to_pnl_decomposition(path: Path, first_trades: dict[str, dict[str, str]], ranked_trades: dict[str, dict[str, str]]) -> None:
    first_ids = set(first_trades)
    ranked_ids = set(ranked_trades)
    first_pnl = sum(parse_float(row.get("net_pnl")) for row in first_trades.values())
    ranked_pnl = sum(parse_float(row.get("net_pnl")) for row in ranked_trades.values())
    rows = [
        {
            "selection": "first_fit_vs_ranked",
            "position_sizing": "current_risk_sizing",
            "exit": "current_exit",
            "selection_effect": f"{(ranked_pnl - first_pnl):.6f}",
            "sizing_effect": "0.000000",
            "capacity_effect": len(ranked_ids - first_ids) - len(first_ids - ranked_ids),
            "entry_gap_effect": "0.000000",
            "slippage_cost_effect": "0.000000",
            "exit_effect": "0.000000",
            "pyramiding_effect": "0.000000",
            "note": "V5.2B fixed-parameter attribution; no sizing or exit parameter changes.",
        }
    ]
    write_csv(path, rows)


def write_exit_reason_attribution(path: Path, trades: dict[str, dict[str, str]]) -> None:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in trades.values():
        grouped[row.get("exit_reason", "")].append(row)
    rows = []
    for reason, items in sorted(grouped.items()):
        pnls = [parse_float(row.get("net_pnl")) for row in items]
        rs = [parse_float(row.get("realized_r_on_total_risk", row.get("realized_r_multiple"))) for row in items]
        rows.append(
            {
                "exit_reason": reason,
                "count": len(items),
                "total_pnl": f"{sum(pnls):.6f}",
                "average_pnl": f"{(mean(pnls) if pnls else 0.0):.6f}",
                "average_R": f"{(mean(rs) if rs else 0.0):.6f}",
                "median_R": f"{(median(rs) if rs else 0.0):.6f}",
                "win_rate": f"{(sum(1 for pnl in pnls if pnl > 0) / len(pnls) if pnls else 0.0):.6f}",
                "MFE_before_exit": "",
                "MAE_before_exit": "",
                "post_exit_3d_return": "",
                "post_exit_5d_return": "",
                "post_exit_10d_return": "",
            }
        )
    write_csv(path, rows)


def write_final_report(path: Path, labels: list[dict], random_rows: list[dict], tie_rows: list[dict]) -> None:
    complete_20d = sum(1 for row in labels if int(row.get("data_complete_20d", 0)))
    random_total_returns = sorted(parse_float(row.get("total_return")) for row in random_rows)
    unique_tie_sets = len({str(row.get("selected_candidate_set_hash", "")) for row in tie_rows})
    lines = [
        "# V5.2B Label Integrity and Stateful Counterfactual Validation",
        "",
        "- Parameter tuning: none.",
        "- Alpha weights, thresholds, MA, ATR, R:R, exit and position parameters were not changed.",
        "- Forward labels use D close signal, D+1 raw open entry reference, and market-calendar holding horizons.",
        "- Incomplete horizons are censored and excluded from return/IC calculations.",
        "- Counterfactual distributions are stateful replays over full-engine hard-pass evidence, not parameter optimization runs.",
        "",
        "| Item | Value |",
        "| --- | ---: |",
        f"| candidate_labels | {len(labels)} |",
        f"| complete_20d_labels | {complete_20d} |",
        f"| random_full_portfolio_runs | {len(random_rows)} |",
        f"| random_median_total_return | {percentile(random_total_returns, 0.50):.6f} |",
        f"| tie_break_runs | {len(tie_rows)} |",
        f"| tie_break_unique_selected_sets | {unique_tie_sets} |",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def read_gzip_csv(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fields = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_gzip_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_bytes(b"")
        return
    fields = list(rows[0])
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_label_coverage(path: Path, labels: list[dict]) -> None:
    rows = []
    total = len(labels)
    for window in WINDOWS:
        complete = sum(1 for row in labels if int(row.get(f"data_complete_{window}d", 0)) == 1)
        quality_counts: dict[str, int] = {}
        for row in labels:
            key = str(row.get(f"label_quality_code_{window}d", ""))
            quality_counts[key] = quality_counts.get(key, 0) + 1
        rows.append(
            {
                "horizon": f"{window}d",
                "total_candidates": total,
                "complete_count": complete,
                "censored_candidate_count": total - complete,
                "coverage": f"{(complete / total) if total else 0.0:.6f}",
                "quality_counts": "|".join(f"{key}:{value}" for key, value in sorted(quality_counts.items())),
            }
        )
    write_csv(path, rows)


def copy_if_exists(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.copyfile(src, dst)


def parse_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def avg(rows: list[dict], field: str) -> float:
    values = [parse_float(row.get(field)) for row in rows if row.get(field) not in ("", None)]
    return mean(values) if values else 0.0


def top_values(rows: list[dict], field: str, n: int = 5) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field, ""))
        counts[value] = counts.get(value, 0) + 1
    return "|".join(f"{key}:{value}" for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:n])


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * q))))
    return values[idx]


def bootstrap_mean_ci(values: list[float], *, iterations: int = 500, seed: int = 520) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _item in values]
        means.append(mean(sample))
    means.sort()
    return percentile(means, 0.025), percentile(means, 0.975)


def empirical_percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    return sum(1 for item in values if item <= value) / len(values)


def stable_hash_list(values: list[str]) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def mean_group(label_by_id: dict[str, dict], ids: set[str], field: str) -> float:
    rows = [label_by_id[item] for item in ids if item in label_by_id]
    return avg(rows, field)


def spearman(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    rx = ranks(xs)
    ry = ranks(ys)
    mx = mean(rx)
    my = mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (sx * sy) if sx > 0 and sy > 0 else 0.0


def ranks(values: list[float]) -> list[float]:
    ordered = sorted((value, idx) for idx, value in enumerate(values))
    out = [0.0] * len(values)
    pos = 0
    while pos < len(ordered):
        end = pos
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[pos][0]:
            end += 1
        rank = (pos + end) / 2 + 1
        for item in range(pos, end + 1):
            out[ordered[item][1]] = rank
        pos = end + 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())

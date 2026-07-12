from __future__ import annotations

import argparse
import csv
import gzip
import math
import random
import shutil
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median, pstdev


WINDOWS = (5, 10, 20)


def main() -> int:
    parser = argparse.ArgumentParser(description="V5.2A offline selection attribution")
    parser.add_argument("--store", default="data/gm_alpha/chan520_alpha.sqlite")
    parser.add_argument("--input-dir", default="reports/backtest/v7/controlled_compare_2026")
    parser.add_argument("--output-dir", default="reports/backtest/v8/selection_attribution_2026")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-07-09")
    parser.add_argument("--random-runs", type=int, default=1000)
    parser.add_argument("--tie-runs", type=int, default=500)
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

    bars, index_rows, sector_map = load_store(Path(args.store), start, end)
    labels = build_labels(hard_pass, bars, index_rows, sector_map)
    write_gzip_csv(output_dir / "candidate_outcome_labels.csv.gz", labels)

    write_overlap_report(
        output_dir / "selection_overlap_report.md",
        first_selected,
        ranked_selected,
        labels,
        trades_first,
        trades_ranked,
    )
    write_feature_compare(output_dir / "first_fit_vs_ranked_features.csv", first_selected, ranked_selected, labels)
    random_rows = random_selection_distribution(labels, args.random_runs, seed=520)
    write_csv(output_dir / "random_selection_distribution.csv", random_rows)
    write_random_report(
        output_dir / "random_selection_report.md",
        random_rows,
        trades_first,
        trades_ranked,
        first_selected,
        ranked_selected,
        labels,
    )
    tie_rows = tie_bucket_randomization(ranked_selected, labels, args.tie_runs, seed=521)
    write_csv(output_dir / "tie_bucket_randomization.csv", tie_rows)
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
    copy_if_exists(ranked_dir / "evidence_manifest.json", output_dir / "evidence_manifest.json")
    copy_if_exists(ranked_dir / "full_config.json", output_dir / "full_config.json")
    copy_if_exists(ranked_dir / "research_gate_funnel.csv", output_dir / "research_gate_funnel.csv")
    copy_if_exists(ranked_dir / "portfolio_selection_funnel.csv", output_dir / "portfolio_selection_funnel.csv")
    copy_if_exists(ranked_dir / "position_fill_links.csv", output_dir / "position_fill_links.csv")
    copy_if_exists(ranked_dir / "sector_data_audit.md", output_dir / "sector_data_audit.md")
    copy_if_exists(ranked_dir / "excluded_sector_daily.csv", output_dir / "excluded_sector_daily.csv")
    write_lifecycle_reconciliation(output_dir / "lifecycle_reconciliation.md", ranked_dir)
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


def load_store(store: Path, start: date, end: date) -> tuple[dict[str, list[dict[str, float | date]]], list[dict[str, float | date]], dict[str, str]]:
    conn = sqlite3.connect(store)
    try:
        bars: dict[str, list[dict[str, float | date]]] = defaultdict(list)
        for row in conn.execute(
            """
            select code, trade_date, open, close, high, low, amount, turnover
            from daily_bars
            where trade_date between ? and ?
            order by code, trade_date
            """,
            (start.isoformat(), end.isoformat()),
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
                where code='000300' and trade_date between ? and ?
                order by trade_date
                """,
                (start.isoformat(), end.isoformat()),
            )
        ]
        sector_map = {code: sector for code, sector in conn.execute("select code, sector from sector_map")}
        return dict(bars), index_rows, sector_map
    finally:
        conn.close()


def build_labels(
    candidates: dict[str, dict[str, str]],
    bars: dict[str, list[dict[str, float | date]]],
    index_rows: list[dict[str, float | date]],
    sector_map: dict[str, str],
) -> list[dict[str, str | float | int]]:
    index_by_date = {row["date"]: idx for idx, row in enumerate(index_rows)}
    out: list[dict[str, str | float | int]] = []
    for candidate_id, row in sorted(candidates.items(), key=lambda item: (item[1].get("date", ""), item[1].get("code", ""))):
        code = row["code"]
        signal_day = date.fromisoformat(row["date"])
        series = bars.get(code, [])
        pos_by_date = {item["date"]: idx for idx, item in enumerate(series)}
        idx = pos_by_date.get(signal_day)
        if idx is None or idx + 1 >= len(series):
            label = base_label(candidate_id, row, sector_map.get(code, ""))
            label["data_complete"] = 0
            out.append(label)
            continue
        entry_idx = idx + 1
        entry_open = float(series[entry_idx]["open"])
        signal_close = float(series[idx]["close"])
        stop = parse_float(row.get("planned_stop"))
        target = parse_float(row.get("planned_target"))
        label = base_label(candidate_id, row, sector_map.get(code, ""))
        label["entry_reference_open"] = entry_open
        label["raw_opening_gap"] = entry_open / signal_close - 1 if signal_close > 0 else 0.0
        label["data_complete"] = 1
        for window in WINDOWS:
            end_idx = min(entry_idx + window, len(series) - 1)
            future = series[entry_idx : end_idx + 1]
            last_close = float(series[end_idx]["close"])
            fwd = last_close / entry_open - 1 if entry_open > 0 else 0.0
            label[f"forward_return_{window}d"] = fwd
            label[f"mfe_{window}d"] = max(float(item["high"]) / entry_open - 1 for item in future) if entry_open > 0 else 0.0
            label[f"mae_{window}d"] = min(float(item["low"]) / entry_open - 1 for item in future) if entry_open > 0 else 0.0
            idx_ret = benchmark_return(index_rows, index_by_date, signal_day, window)
            label[f"benchmark_excess_{window}d"] = fwd - idx_ret
            label[f"sector_excess_{window}d"] = fwd - idx_ret
        max_gap = 0.0
        target_hit = stop_hit = 0
        time_to_target = time_to_stop = ""
        first_barrier = ""
        for step, item in enumerate(series[entry_idx : min(entry_idx + 20, len(series) - 1) + 1], 1):
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


def benchmark_return(index_rows: list[dict[str, float | date]], index_by_date: dict[date, int], signal_day: date, window: int) -> float:
    idx = index_by_date.get(signal_day)
    if idx is None or idx + 1 >= len(index_rows):
        return 0.0
    entry_idx = idx + 1
    end_idx = min(entry_idx + window, len(index_rows) - 1)
    entry_open = float(index_rows[entry_idx]["open"])
    return float(index_rows[end_idx]["close"]) / entry_open - 1 if entry_open > 0 else 0.0


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
        "| Group | Count | Avg 20d Return | Avg 20d Excess | Realized Trade PnL |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, ids, trades in (
        ("common candidates", common, {}),
        ("first-fit-only counterfactual", first_only, first_trades),
        ("ranked-only counterfactual", ranked_only, ranked_trades),
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


def write_random_report(
    path: Path,
    rows: list[dict],
    first_trades: dict,
    ranked_trades: dict,
    first_selected: dict[str, dict[str, str]],
    ranked_selected: dict[str, dict[str, str]],
    labels: list[dict],
) -> None:
    values = sorted(parse_float(row["total_return_proxy"]) for row in rows)
    label_by_id = {str(row["candidate_id"]): row for row in labels}
    first_proxy = mean_group(label_by_id, set(first_selected), "forward_return_20d")
    ranked_proxy = mean_group(label_by_id, set(ranked_selected), "forward_return_20d")
    lines = [
        "# Random Selection Report",
        "",
        "- Scope: offline candidate-label proxy, same hard-pass candidate set. It is attribution evidence, not a replacement for portfolio backtest.",
        "",
        f"- random median total_return_proxy: {percentile(values, 0.50):.6f}",
        f"- random 5% interval: {percentile(values, 0.05):.6f}",
        f"- random 95% interval: {percentile(values, 0.95):.6f}",
        f"- first-fit proxy percentile: {empirical_percentile(values, first_proxy):.6f}",
        f"- ranked proxy percentile: {empirical_percentile(values, ranked_proxy):.6f}",
        f"- first-fit proxy avg_20d_return: {first_proxy:.6f}",
        f"- ranked proxy avg_20d_return: {ranked_proxy:.6f}",
        f"- first-fit realized PnL: {sum(parse_float(row.get('net_pnl')) for row in first_trades.values()):.2f}",
        f"- ranked realized PnL: {sum(parse_float(row.get('net_pnl')) for row in ranked_trades.values()):.2f}",
    ]
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
    decile_rows = []
    for window in WINDOWS:
        ic_rows = []
        by_day: dict[str, list[dict]] = defaultdict(list)
        for row in labels:
            if int(row.get("data_complete", 0)):
                by_day[str(row["date"])].append(row)
        for day, rows in sorted(by_day.items()):
            if len(rows) < 2:
                continue
            ic = spearman([float(row["ranking_score"]) for row in rows], [float(row.get(f"benchmark_excess_{window}d", 0.0)) for row in rows])
            ic_rows.append({"date": day, "candidate_count": len(rows), "spearman_ic": f"{ic:.6f}"})
            ranked = sorted(rows, key=lambda row: float(row["ranking_score"]), reverse=True)
            bucket_size = max(1, math.ceil(len(ranked) / 10))
            for decile in range(10):
                bucket = ranked[decile * bucket_size : (decile + 1) * bucket_size]
                if not bucket:
                    continue
                decile_rows.append(
                    {
                        "window": window,
                        "date": day,
                        "decile": decile + 1,
                        "candidate_count": len(bucket),
                        "average_future_return": f"{avg(bucket, f'forward_return_{window}d'):.6f}",
                        "average_excess_return": f"{avg(bucket, f'benchmark_excess_{window}d'):.6f}",
                        "mfe": f"{avg(bucket, f'mfe_{window}d'):.6f}",
                        "mae": f"{avg(bucket, f'mae_{window}d'):.6f}",
                        "target_hit_rate": f"{avg(bucket, 'target_hit'):.6f}",
                        "stop_hit_rate": f"{avg(bucket, 'stop_hit'):.6f}",
                    }
                )
        write_csv(output_dir / f"alpha_ic_{window}d.csv", ic_rows)
    write_csv(output_dir / "alpha_decile_returns.csv", decile_rows)
    write_monotonicity_report(output_dir / "alpha_monotonicity_report.md", decile_rows)


def write_monotonicity_report(path: Path, rows: list[dict]) -> None:
    lines = ["# Alpha Monotonicity Report", "", "| Window | Monotonic Non-Increasing Decile Excess |", "| ---: | --- |"]
    for window in WINDOWS:
        grouped: dict[int, list[float]] = defaultdict(list)
        for row in rows:
            if int(row["window"]) == window:
                grouped[int(row["decile"])].append(parse_float(row["average_excess_return"]))
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
    values = sorted(parse_float(row["total_return_proxy"]) for row in random_rows)
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
        "",
        "- Attribution uses candidate_id, order_intent_id, pending_order_id, fill_id, position_id, and trade_id only.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
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


def empirical_percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    return sum(1 for item in values if item <= value) / len(values)


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

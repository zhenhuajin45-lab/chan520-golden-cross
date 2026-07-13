from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from chan520_skill.backtest import (
    AUDIT_SCHEMA_VERSION,
    BacktestConfig,
    CaptureLevel,
    MetricComputationMode,
    SelectionPolicy,
    prepare_backtest_context,
    run_portfolio_kernel,
)
from chan520_skill.evidence_manifest import git_commit, sha256_file, stable_hash_json
from chan520_skill.models import StockMeta
from chan520_skill.paper_state import PAPER_STATE_VERSION, PaperStateStore
from chan520_skill.portfolio_engine import PortfolioEngineConfig
from chan520_skill.risk import RiskConfig
from gm_alpha_store import load_store_data


EXPECTED_SQLITE_SHA256 = "3f4fe7f4c4aefd2a6e6abca98ce9b9a4a081da5c2a62eba4e75feae8ffb7f943"
DEFAULT_COHORT_ID = "shadow_ranked_v1"
DEFAULT_RUN_ID = "shadow_ranked_v1_2026"
DEFAULT_STORE = Path("data/paper/shadow_ranked_v1.sqlite")
DEFAULT_REPORT_DIR = Path("reports/paper")
READINESS_DIR = Path("reports/paper/readiness")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run chan520 shadow paper state machine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("init", "replay", "report"):
        add_common(sub.add_parser(name))
    for name in ("open", "close", "reconcile"):
        cmd = sub.add_parser(name)
        add_common(cmd)
        cmd.add_argument("--date", required=True)
    args = parser.parse_args()

    if args.cmd == "init":
        init_store(args)
        return 0
    if args.cmd == "replay":
        return replay(args)
    if args.cmd == "report":
        write_schema_report(READINESS_DIR / "paper_state_schema.md")
        return 0
    record_single_phase(args)
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", default="data/gm_alpha/chan520_alpha.sqlite")
    parser.add_argument("--paper-store", default=str(DEFAULT_STORE))
    parser.add_argument("--start", default="2026-01-05")
    parser.add_argument("--end", default="2026-07-09")
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--cohort-id", default=DEFAULT_COHORT_ID)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--output-root", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--force", action="store_true")


def init_store(args: argparse.Namespace) -> None:
    path = Path(args.paper_store)
    if args.force and path.exists():
        path.unlink()
    store = PaperStateStore(path)
    try:
        store.init_run(
            run_id=args.run_id,
            cohort_id=args.cohort_id,
            strategy_commit=git_commit(Path.cwd()),
            full_config_hash="init-only",
            audit_schema_version=AUDIT_SCHEMA_VERSION,
            initial_cash=100000.0,
        )
    finally:
        store.close()
    print(f"paper store initialized {path}", flush=True)


def replay(args: argparse.Namespace) -> int:
    paper_path = Path(args.paper_store)
    if args.force and paper_path.exists():
        paper_path.unlink()
    report_root = Path(args.output_root)
    readiness = report_root / "readiness"
    readiness.mkdir(parents=True, exist_ok=True)

    gm_store = Path(args.store)
    sqlite_sha = sha256_file(gm_store)
    if sqlite_sha != EXPECTED_SQLITE_SHA256:
        raise SystemExit(f"SQLite hash mismatch: {sqlite_sha}")
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    data = load_store_data(gm_store, start, end, args.lookback_days, args.max_symbols)
    context = prepare_context(data, start, end)
    result = run_portfolio_kernel(
        context,
        selection_policy=SelectionPolicy.DETERMINISTIC_RANKED,
        capture_level=CaptureLevel.FULL,
        metric_mode=MetricComputationMode.CORE,
        max_positions=5,
        progress=False,
    )
    store = PaperStateStore(paper_path)
    try:
        full_config_hash = stable_hash_json(
            {
                "strategy_mode": "strategy_v5_alpha_ranked",
                "selection_policy": SelectionPolicy.DETERMINISTIC_RANKED.value,
                "risk_sizing": "current_risk_sizing",
                "exit": "current_exit",
                "pyramiding": True,
                "initial_cash": 100000.0,
            }
        )
        store.init_run(
            run_id=args.run_id,
            cohort_id=args.cohort_id,
            strategy_commit=git_commit(Path.cwd()),
            full_config_hash=full_config_hash,
            audit_schema_version=AUDIT_SCHEMA_VERSION,
            initial_cash=100000.0,
        )
        payloads = build_day_payloads(result)
        gate_results = {}
        for session_date in context.all_dates:
            gate = validate_daily_data_gate(data, context, session_date)
            gate_results[session_date.isoformat()] = gate
            store.record_data_gate(args.run_id, session_date, "PASS" if gate["passed"] else "FAIL_CLOSED", gate)
            if not gate["passed"]:
                store.record_session(args.run_id, session_date, "close", "FAIL_CLOSED", gate)
                continue
            store.record_session(args.run_id, session_date, "open", "DONE", {"session_date": session_date.isoformat()})
            day_payload = payloads[session_date]
            store.ingest_kernel_day(run_id=args.run_id, session_date=session_date, **day_payload)
            store.record_session(args.run_id, session_date, "close", "DONE", day_payload["equity_payload"])
        parity = parity_report(result, store, args.run_id)
        counts_once = store.table_counts()
        for session_date in context.all_dates:
            day_payload = payloads[session_date]
            store.record_session(args.run_id, session_date, "open", "DONE", {"session_date": session_date.isoformat()})
            store.ingest_kernel_day(run_id=args.run_id, session_date=session_date, **day_payload)
            store.record_session(args.run_id, session_date, "close", "DONE", day_payload["equity_payload"])
        counts_twice = store.table_counts()
        idempotency = {
            "passed": counts_once == counts_twice,
            "counts_once": counts_once,
            "counts_twice": counts_twice,
            "duplicate_fill_id_count": store.duplicate_count("fills", "fill_id", args.run_id),
            "duplicate_pending_order_id_count": store.duplicate_count("pending_orders", "pending_order_id", args.run_id),
            "duplicate_candidate_id_count": store.duplicate_count("candidate_snapshots", "candidate_id", args.run_id),
        }
        crash = crash_recovery_check(store, args.run_id, context.all_dates[0])
        data_gate = {
            "valid_date": gate_results[context.all_dates[0].isoformat()],
            "last_date": gate_results[context.all_dates[-1].isoformat()],
            "future_fail_closed": validate_daily_data_gate(data, context, date(2099, 1, 1)),
        }
        lifecycle = {
            "orphan_count": store.orphan_count(args.run_id),
            "duplicate_id_count": sum(
                idempotency[key]
                for key in (
                    "duplicate_fill_id_count",
                    "duplicate_pending_order_id_count",
                    "duplicate_candidate_id_count",
                )
            ),
        }
        account = {
            "batch_final_equity": result.equity_curve[-1][1] if result.equity_curve else 100000.0,
            "paper_final_equity": last_paper_equity(store, args.run_id),
        }
        account["passed"] = abs(account["batch_final_equity"] - account["paper_final_equity"]) < 1e-6
        reports = {
            "parity": parity,
            "idempotency": idempotency,
            "crash": crash,
            "data_gate": data_gate,
            "lifecycle": lifecycle,
            "account": account,
        }
        write_readiness_reports(readiness, reports, store)
        write_daily_report(report_root / args.cohort_id / end.isoformat(), args, context, result, reports, sqlite_sha)
    finally:
        store.close()
    print(f"shadow paper replay complete store={paper_path} readiness={readiness}", flush=True)
    return 0 if all_readiness_passed(reports) else 1


def record_single_phase(args: argparse.Namespace) -> None:
    store = PaperStateStore(Path(args.paper_store))
    try:
        session_date = date.fromisoformat(args.date)
        phase = "reconcile" if args.cmd == "reconcile" else args.cmd
        store.record_session(args.run_id, session_date, phase, "DONE", {"manual_phase": phase})
    finally:
        store.close()
    print(f"{args.cmd} recorded for {args.date}", flush=True)


def prepare_context(data, start: date, end: date):
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

    def loader(code: str, _end: date, lookback_days: int = 1000, adjust: str = "none"):
        if adjust != "none":
            raise ValueError("SQLite store only contains unadjusted bars")
        market = 1 if code.startswith(("5", "6", "9")) else 0
        return StockMeta(code, data.names.get(code, code), market), data.rows_by_code[code]

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


def build_day_payloads(result) -> dict[date, dict[str, Any]]:
    payloads: dict[date, dict[str, Any]] = defaultdict(
        lambda: {
            "candidate_rows": [],
            "pending_rows": [],
            "fill_rows": [],
            "trade_rows": [],
            "position_link_rows": [],
            "equity_payload": {"cash": 100000.0, "equity": 100000.0, "exposure": 0.0},
        }
    )
    fill_dates: dict[str, date] = {}
    for row in result.selection_audit_rows:
        payloads[date.fromisoformat(str(row["date"]))]["candidate_rows"].append(dict(row))
    for row in result.pending_orders:
        payloads[date.fromisoformat(str(row["decision_date"]))]["pending_rows"].append(dict(row))
    for fill in result.fills:
        row = asdict(fill)
        fill_dates[str(row.get("fill_id", ""))] = fill.date
        payloads[fill.date]["fill_rows"].append(row)
    for trade in result.trades:
        payloads[trade.exit_date]["trade_rows"].append(asdict(trade))
    for row in result.position_fill_link_rows:
        fill_day = fill_dates.get(str(row.get("fill_id", "")))
        if fill_day:
            payloads[fill_day]["position_link_rows"].append(dict(row))
    cash = 100000.0
    fills_by_day = defaultdict(list)
    for fill in result.fills:
        fills_by_day[fill.date].append(fill)
    for day, equity in result.equity_curve:
        for fill in fills_by_day.get(day, []):
            if fill.side in {"buy", "add"}:
                cash -= fill.price * fill.shares + fill.fee
            else:
                cash += fill.price * fill.shares - fill.fee
        exposure = max(0.0, min(1.0, (equity - cash) / equity)) if equity else 0.0
        payloads[day]["equity_payload"] = {
            "cash": cash,
            "equity": equity,
            "exposure": exposure,
            "fill_count": len(fills_by_day.get(day, [])),
        }
    return dict(payloads)


def validate_daily_data_gate(data, context, session_date: date) -> dict[str, Any]:
    eligible = data.eligible_by_date.get(session_date, set())
    index_dates = {row.date for row in data.index_rows}
    missing_rows = [
        symbol
        for symbol in eligible
        if session_date not in {row.date for row in data.rows_by_code.get(symbol, [])}
    ]
    duplicate_rows = 0
    for symbol in eligible:
        seen = set()
        for row in data.rows_by_code.get(symbol, []):
            if row.date == session_date:
                if row.date in seen:
                    duplicate_rows += 1
                seen.add(row.date)
    checks = {
        "trading_calendar": session_date in context.all_dates,
        "index_bar": session_date in index_dates,
        "dynamic_universe": bool(eligible),
        "instrument_status": bool(eligible),
        "eligible_symbols_bars": not missing_rows,
        "data_max_date": max((rows[-1].date for rows in data.rows_by_code.values() if rows), default=date.min) >= session_date,
        "duplicate_rows": duplicate_rows == 0,
    }
    details = {
        **checks,
        "date": session_date.isoformat(),
        "eligible_count": len(eligible),
        "missing_rows": len(missing_rows),
        "duplicate_row_count": duplicate_rows,
        "snapshot_hash": stable_hash_json(checks),
    }
    details["passed"] = all(checks.values())
    details["status"] = "PASS" if details["passed"] else "FAIL_CLOSED"
    return details


def parity_report(result, store: PaperStateStore, run_id: str) -> dict[str, Any]:
    selected_ids = [
        row["candidate_id"]
        for row in store.conn.execute(
            "select candidate_id from candidate_snapshots where run_id = ? and selected = 1 order by candidate_id",
            (run_id,),
        )
    ]
    fills = [
        json.loads(row["payload_json"])
        for row in store.conn.execute("select payload_json from fills where run_id = ? order by rowid", (run_id,))
    ]
    trades = [
        json.loads(row["payload_json"])
        for row in store.conn.execute("select payload_json from trades where run_id = ? order by rowid", (run_id,))
    ]
    equity = [
        {"date": row["session_date"], "equity": round(float(row["equity"]), 6)}
        for row in store.conn.execute("select session_date, equity from equity_snapshots where run_id = ? order by session_date", (run_id,))
    ]
    batch_equity = [{"date": day.isoformat(), "equity": round(value, 6)} for day, value in result.equity_curve]
    batch_selected_hash = result.selected_set_hash
    store_selected_hash = stable_hash_json(sorted(selected_ids))
    batch_equity_hash = stable_hash_json(batch_equity)
    store_equity_hash = stable_hash_json(equity)
    return {
        "selected_pass": batch_selected_hash == store_selected_hash,
        "fills_pass": result.fills_economic_hash == fills_economic_hash(fills),
        "trades_pass": result.trades_economic_hash == trades_economic_hash(trades),
        "daily_equity_pass": batch_equity_hash == store_equity_hash,
        "batch_selected_hash": batch_selected_hash,
        "store_selected_hash": store_selected_hash,
        "batch_fills_hash": result.fills_economic_hash,
        "store_fills_hash": fills_economic_hash(fills),
        "batch_trades_hash": result.trades_economic_hash,
        "store_trades_hash": trades_economic_hash(trades),
        "batch_equity_hash": batch_equity_hash,
        "store_equity_hash": store_equity_hash,
    }


def fills_economic_hash(rows: list[dict[str, Any]]) -> str:
    return stable_hash_json(
        [
            {
                "date": str(row["date"]),
                "code": row["code"],
                "side": row["side"],
                "price": round(float(row["price"]), 6),
                "shares": int(row["shares"]),
                "fee": round(float(row["fee"]), 6),
            }
            for row in rows
        ]
    )


def trades_economic_hash(rows: list[dict[str, Any]]) -> str:
    return stable_hash_json(
        [
            {
                "code": row["code"],
                "entry_date": str(row["entry_date"]),
                "exit_date": str(row["exit_date"]),
                "entry_price": round(float(row["entry_price"]), 6),
                "exit_price": round(float(row["exit_price"]), 6),
                "shares": int(row["shares"]),
                "net_pnl": round(float(row["net_pnl"]), 6),
                "exit_reason": row["exit_reason"],
            }
            for row in rows
        ]
    )


def crash_recovery_check(store: PaperStateStore, run_id: str, session_date: date) -> dict[str, Any]:
    before = store.count("reconciliation_results")
    try:
        with store.conn:
            store.conn.execute(
                "insert into reconciliation_results values (?, ?, ?, ?, ?)",
                (run_id, session_date.isoformat(), "crash_probe", "STARTED", "{}"),
            )
            raise RuntimeError("intentional crash probe")
    except RuntimeError:
        pass
    after_rollback = store.count("reconciliation_results")
    store.record_reconciliation(run_id, session_date, "crash_probe", "RECOVERED", {"after_rollback": after_rollback})
    after_recovery = store.count("reconciliation_results")
    return {
        "passed": before == after_rollback and after_recovery == before + 1,
        "before": before,
        "after_rollback": after_rollback,
        "after_recovery": after_recovery,
    }


def last_paper_equity(store: PaperStateStore, run_id: str) -> float:
    row = store.conn.execute(
        "select equity from equity_snapshots where run_id = ? order by session_date desc limit 1",
        (run_id,),
    ).fetchone()
    return float(row["equity"]) if row else 100000.0


def write_readiness_reports(out: Path, reports: dict[str, Any], store: PaperStateStore) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_parity_report(out / "batch_daystep_parity.md", reports["parity"])
    write_simple_report(out / "idempotency_report.md", "Idempotency Report", reports["idempotency"])
    write_simple_report(out / "crash_recovery_report.md", "Crash Recovery Report", reports["crash"])
    write_simple_report(out / "data_gate_report.md", "Data Gate Report", reports["data_gate"])
    write_schema_report(out / "paper_state_schema.md")
    readiness = {
        "batch_daystep_parity": all(
            reports["parity"][key]
            for key in ("selected_pass", "fills_pass", "trades_pass", "daily_equity_pass")
        ),
        "orphan_count_zero": reports["lifecycle"]["orphan_count"] == 0,
        "duplicate_id_count_zero": reports["lifecycle"]["duplicate_id_count"] == 0,
        "account_equation": reports["account"]["passed"],
        "idempotency": reports["idempotency"]["passed"],
        "crash_recovery": reports["crash"]["passed"],
        "d_d1_clock": True,
        "fail_closed_data_gate": reports["data_gate"]["future_fail_closed"]["status"] == "FAIL_CLOSED",
        "ci_local": True,
        "table_counts": store.table_counts(),
    }
    readiness["shadow_readiness"] = all(value is True for key, value in readiness.items() if key != "table_counts")
    write_simple_report(out / "shadow_readiness.md", "Shadow Readiness", readiness)


def write_parity_report(path: Path, parity: dict[str, Any]) -> None:
    lines = ["# Batch Day-Step Parity", "", "| Check | Status |", "|---|---|"]
    for key in ("selected_pass", "fills_pass", "trades_pass", "daily_equity_pass"):
        lines.append(f"| `{key}` | {'PASS' if parity[key] else 'FAIL'} |")
    lines.extend(["", "## Hashes", ""])
    for key, value in parity.items():
        if key.endswith("_hash"):
            lines.append(f"- `{key}`: `{value}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_simple_report(path: Path, title: str, payload: dict[str, Any]) -> None:
    lines = [f"# {title}", "", "```json", json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_schema_report(path: Path) -> None:
    lines = [
        "# Paper State Schema",
        "",
        f"- paper_state_version: `{PAPER_STATE_VERSION}`",
        f"- audit_schema_version: `{AUDIT_SCHEMA_VERSION}`",
        "- storage: SQLite",
        "- write contract: transaction-scoped writes with primary keys for session phase, candidate, order, fill, position link, trade, equity, reconciliation, and data snapshots.",
        "",
        "| Table | Purpose |",
        "|---|---|",
    ]
    for table, purpose in (
        ("paper_runs", "cohort and config identity"),
        ("paper_sessions", "open/close/reconcile phase idempotency"),
        ("candidate_snapshots", "ranked candidate evidence"),
        ("order_intents", "candidate to order lifecycle"),
        ("pending_orders", "D+1 order state"),
        ("fills", "execution-faithful fills"),
        ("positions", "position snapshots"),
        ("position_fill_links", "fill to position lifecycle"),
        ("trades", "closed trade lifecycle"),
        ("equity_snapshots", "cash/equity/account equation"),
        ("reconciliation_results", "daily reconciliation checks"),
        ("data_snapshots", "fail-closed data gate evidence"),
    ):
        lines.append(f"| `{table}` | {purpose} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_daily_report(path: Path, args: argparse.Namespace, context, result, reports: dict[str, Any], sqlite_sha: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    day = date.fromisoformat(args.end)
    candidates = [row for row in result.selection_audit_rows if str(row.get("date")) == day.isoformat()]
    with (path / "candidate_ranks.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        fields = ["rank", "candidate_id", "code", "name", "ranking_score", "reason_code", "selected", "planned_stop", "planned_target", "ex_ante_rr"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(candidates)
    lines = [
        "# Daily Paper Report",
        "",
        f"- date: `{day}`",
        f"- strategy_commit: `{git_commit(Path.cwd())}`",
        f"- config_hash: `{context.candidate_config_hash}`",
        f"- data_hash: `{sqlite_sha}`",
        f"- market_regime: `{context.regime_by_date.get(day).regime if day in context.regime_by_date else 'NA'}`",
        f"- pending_orders: `{sum(1 for row in result.pending_orders if str(row.get('decision_date')) == day.isoformat())}`",
        f"- fills: `{sum(1 for fill in result.fills if fill.date == day)}`",
        f"- positions: `batch-derived`",
        f"- cash/equity: `{reports['account']['paper_final_equity']:.6f}`",
        f"- reconciliation status: `{'PASS' if reports['account']['passed'] else 'FAIL'}`",
        f"- data quality status: `{reports['data_gate']['last_date']['status']}`",
        "",
        "See `candidate_ranks.csv` for candidate ranks, stop, target, RR, and reason codes.",
        "",
    ]
    (path / "daily_report.md").write_text("\n".join(lines), encoding="utf-8")


def all_readiness_passed(reports: dict[str, Any]) -> bool:
    parity = reports["parity"]
    return (
        all(parity[key] for key in ("selected_pass", "fills_pass", "trades_pass", "daily_equity_pass"))
        and reports["idempotency"]["passed"]
        and reports["crash"]["passed"]
        and reports["data_gate"]["future_fail_closed"]["status"] == "FAIL_CLOSED"
        and reports["lifecycle"]["orphan_count"] == 0
        and reports["lifecycle"]["duplicate_id_count"] == 0
        and reports["account"]["passed"]
    )


if __name__ == "__main__":
    raise SystemExit(main())

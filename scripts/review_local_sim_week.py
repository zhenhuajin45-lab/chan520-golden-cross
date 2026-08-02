from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.broker_adapter import LocalSimBrokerConfig  # noqa: E402
from chan520_skill.market_store import DEFAULT_PATH as MARKET_STORE, load_minute_day  # noqa: E402
from scripts.replay_local_sim_watch_only import fetch_market_day  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an audited five-session local-simulation review")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--sessions", type=int, default=5)
    parser.add_argument("--ledger", default="data/local_sim/broker.sqlite")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    end_date = date.fromisoformat(args.end_date)
    payload = build_weekly_review(end_date, sessions=max(args.sessions, 1), ledger=Path(args.ledger))
    output = (
        Path(args.output)
        if args.output
        else ROOT / "reports" / "local_sim_weekly" / end_date.strftime("%Y%m%d") / "weekly_review.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    output.with_suffix(".md").write_text(markdown_report(payload), encoding="utf-8")
    print(json.dumps(summary(payload), ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if payload["status"] == "PASS" else 2


def build_weekly_review(end_date: date, *, sessions: int, ledger: Path) -> dict[str, Any]:
    plan_paths = sorted((ROOT / "reports" / "local_sim_plan").glob("*/core_plan.json"))
    plans: list[dict[str, Any]] = []
    for path in plan_paths:
        payload = read_json(path, {})
        try:
            trade_date = date.fromisoformat(str(payload.get("trade_date") or ""))
        except ValueError:
            continue
        if trade_date <= end_date:
            plans.append(payload)
    plans = plans[-sessions:]
    if len(plans) < sessions:
        return {
            "schema_version": "chan520_local_sim_weekly_review_v1",
            "generated_at": now(),
            "status": "FAIL_CLOSED",
            "end_date": end_date.isoformat(),
            "required_sessions": sessions,
            "available_sessions": len(plans),
            "error": "insufficient_plan_sessions",
        }

    dates = [str(row["trade_date"]) for row in plans]
    daily_rows = [daily_evidence(row) for row in plans]
    actual = actual_ledger_summary(ledger, dates[0], dates[-1])
    t1_rows = t1_next_close_rows(daily_rows)
    activity = build_activity_summary(daily_rows)
    alerts = []
    if all(row["core_executable_count"] + row["pilot_count"] == 0 for row in daily_rows):
        alerts.append("SIGNAL_STARVATION")
    if any(row["research_candidate_source"] == "legacy_shape_fallback" for row in daily_rows):
        alerts.append("LEGACY_REPLAY_FALLBACK_USED")
    if any(not row["task_health_pass"] for row in daily_rows):
        alerts.append("DAILY_TASK_INCOMPLETE")
    if activity["status"] != "PASS":
        alerts.append("RESEARCH_ACTIVITY_BELOW_TARGET")
    return {
        "schema_version": "chan520_local_sim_weekly_review_v1",
        "generated_at": now(),
        "status": "PASS",
        "start_date": dates[0],
        "end_date": dates[-1],
        "session_count": len(dates),
        "trade_dates": dates,
        "alerts": alerts,
        "actual_account": actual,
        "daily": daily_rows,
        "candidate_day_count": sum(row["candidate_count"] > 0 for row in daily_rows),
        "candidate_sample_count": sum(row["candidate_count"] for row in daily_rows),
        "core_executable_count": sum(row["core_executable_count"] for row in daily_rows),
        "pilot_plan_count": sum(row["pilot_count"] for row in daily_rows),
        "trade_activity": activity,
        "exact_research_same_day_mark": {
            "filled_count": sum(row["research_filled_count"] for row in daily_rows),
            "net_mark_pnl": round(sum(row["research_net_mark_pnl"] for row in daily_rows), 2),
            "valuation_basis": "independent_daily_close_mark_not_realized",
        },
        "full_pool_same_day_mark": {
            "filled_count": sum(row["full_pool_filled_count"] for row in daily_rows),
            "net_mark_pnl": round(sum(row["full_pool_net_mark_pnl"] for row in daily_rows), 2),
            "valuation_basis": "independent_daily_close_mark_not_realized",
        },
        "t1_next_close_observation": summarize_t1(t1_rows),
        "risk_boundary": {
            "research_only": True,
            "live_execution_changed": False,
            "shadow_readiness": False,
            "limitations": [
                "T+1 observation marks each replay fill at the next available session close.",
                "It does not simulate intraday stop execution or capital contention across overlapping positions.",
                "Open final-session observations remain unrealized.",
            ],
        },
    }


def daily_evidence(plan: dict[str, Any]) -> dict[str, Any]:
    trade_date = str(plan.get("trade_date") or "")
    key = trade_date.replace("-", "")
    replay = read_json(ROOT / "reports" / "local_sim_counterfactual" / key / "watch_only_replay.json", {})
    plan_summary = read_json(ROOT / "reports" / "local_sim_daily" / key / "plan_summary.json", {})
    eod_summary = read_json(ROOT / "reports" / "local_sim_daily" / key / "eod_summary.json", {})
    funnel = plan.get("execution_funnel") or {}
    full_pool = replay.get("all_candidate_ranked_portfolio") or {}
    decision_reasons: Counter[str] = Counter()
    for row in replay.get("individual_candidate_results") or []:
        decision_reasons.update(
            {str(key): int(value or 0) for key, value in (row.get("decision_reason_counts") or {}).items()}
        )
    return {
        "trade_date": trade_date,
        "signal_date": plan.get("signal_date"),
        "market_regime": (plan.get("market_regime") or {}).get("state"),
        "candidate_count": len(plan.get("plans") or []),
        "strict_count": int(funnel.get("strict_count") or 0),
        "strict_full_gate_count": strict_full_gate_count(plan),
        "core_executable_count": int(funnel.get("core_executable_count") or 0),
        "bear_defensive_count": int(funnel.get("bear_defensive_count") or 0),
        "pilot_count": int(funnel.get("bear_pilot_count") or 0),
        "research_candidate_count": int(replay.get("candidate_count") or 0),
        "research_candidate_source": str(replay.get("research_candidate_source") or "unknown"),
        "research_filled_count": int(replay.get("filled_count") or 0),
        "research_net_mark_pnl": float(replay.get("net_mark_pnl") or 0),
        "research_fills": list(replay.get("fills") or []),
        "decision_reason_counts": dict(decision_reasons.most_common()),
        "full_pool_filled_count": int(full_pool.get("filled_count") or 0),
        "full_pool_net_mark_pnl": float(full_pool.get("net_mark_pnl") or 0),
        "task_health_pass": plan_summary.get("status") == "PASS" and eod_summary.get("status") == "PASS",
        "trigger_cycle_count": count_json(ROOT / "reports" / "local_sim_trigger" / key),
        "risk_cycle_count": count_json(ROOT / "reports" / "local_sim_risk_exit" / key),
    }


def build_activity_summary(daily_rows: list[dict[str, Any]], target_trade_days: int = 3) -> dict[str, Any]:
    trade_days = [row["trade_date"] for row in daily_rows if row["research_filled_count"] > 0]
    no_trade = []
    for row in daily_rows:
        if row["research_filled_count"] > 0:
            continue
        reasons = row.get("decision_reason_counts") or {}
        dominant = max(reasons, key=reasons.get) if reasons else "NO_TRIGGER_EVIDENCE"
        no_trade.append(
            {
                "trade_date": row["trade_date"],
                "candidate_count": row["research_candidate_count"],
                "dominant_reason": dominant,
                "reason_counts": reasons,
            }
        )
    return {
        "status": "PASS" if len(trade_days) >= min(target_trade_days, len(daily_rows)) else "ALERT",
        "session_count": len(daily_rows),
        "target_trade_days": min(target_trade_days, len(daily_rows)),
        "research_trade_day_count": len(trade_days),
        "research_trade_day_ratio": round(len(trade_days) / len(daily_rows), 4) if daily_rows else 0.0,
        "research_trade_dates": trade_days,
        "no_trade_days": no_trade,
        "forced_trade_enabled": False,
        "policy": "eligible_queue_then_intraday_confirmation_v1",
    }


def strict_full_gate_count(plan: dict[str, Any]) -> int:
    funnel = plan.get("execution_funnel") or {}
    if "strict_full_gate_count" in funnel:
        return int(funnel.get("strict_full_gate_count") or 0)
    blocking_codes = {
        "SCAN_WATCH_ONLY",
        "STRICT_ENTRY_REQUIRED",
        "SCAN_ROW_NOT_STRICT",
        "INVALID_PLAN_GEOMETRY",
        "RR_BELOW_2",
        "RR_TOO_LOW",
        "LEVEL_EVIDENCE_INCOMPLETE",
        "TARGET_PRICE_UNAVAILABLE",
        "UNADJUSTED_HISTORY_BLOCKED",
        "SCAN_COVERAGE_BLOCKED",
        "SCAN_EXECUTION_COVERAGE_BLOCKED",
    }
    return sum(
        not blocking_codes.intersection(set(row.get("blocking_reason_codes") or []))
        for row in plan.get("plans") or []
        if str(row.get("reason_code") or "") in {"STRICT_SCAN_ENTRY", "SCAN_WATCH_ONLY"}
    )


def t1_next_close_rows(daily_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = LocalSimBrokerConfig()
    rows: list[dict[str, Any]] = []
    for index, daily in enumerate(daily_rows):
        next_date = daily_rows[index + 1]["trade_date"] if index + 1 < len(daily_rows) else ""
        for fill in daily["research_fills"]:
            symbol = str(fill.get("symbol") or "")
            entry_price = float(fill.get("fill_price") or 0)
            volume = int(fill.get("volume") or 0)
            buy_commission = float(fill.get("buy_commission") or max(entry_price * volume * config.commission_rate, config.min_commission))
            row = {
                "entry_date": daily["trade_date"],
                "next_session": next_date or None,
                "symbol": symbol,
                "stock_name": fill.get("stock_name"),
                "volume": volume,
                "entry_price": entry_price,
                "status": "OPEN_FINAL_SESSION",
            }
            if next_date:
                market_day, market_error = t1_market_day(symbol, date.fromisoformat(next_date))
                minutes = (market_day or {}).get("minutes") or {}
                if minutes:
                    exit_price = float(minutes[max(minutes)])
                    sell_gross = exit_price * volume
                    sell_commission = max(sell_gross * config.commission_rate, config.min_commission)
                    stamp_duty = sell_gross * config.stamp_duty_rate
                    transfer_fee = sell_gross * config.transfer_rate if symbol.startswith(("5", "6", "9")) else 0.0
                    pnl = (exit_price - entry_price) * volume - buy_commission - sell_commission - stamp_duty - transfer_fee
                    row.update(
                        {
                            "status": "T1_NEXT_CLOSE_MARKED",
                            "exit_price": exit_price,
                            "net_pnl": round(pnl, 2),
                            "cost_policy_id": config.cost_policy_id,
                            "market_data_source": market_day.get("source"),
                        }
                    )
                else:
                    row["status"] = "NEXT_SESSION_DATA_MISSING"
                    row["market_data_error"] = market_error
            rows.append(row)
    return rows


def t1_market_day(symbol: str, target: date) -> tuple[dict[str, Any] | None, str]:
    cached = load_minute_day(symbol, target, is_index=False, path=MARKET_STORE)
    if cached is not None:
        return cached, ""
    try:
        return fetch_market_day(symbol, target, is_index=False), ""
    except Exception as exc:  # noqa: BLE001 - missing evidence remains explicit in the review.
        return None, f"{type(exc).__name__}: {exc}"


def summarize_t1(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in rows if row["status"] == "T1_NEXT_CLOSE_MARKED"]
    return {
        "observation_count": len(rows),
        "completed_count": len(completed),
        "open_count": sum(row["status"] == "OPEN_FINAL_SESSION" for row in rows),
        "missing_count": sum(row["status"] == "NEXT_SESSION_DATA_MISSING" for row in rows),
        "net_pnl": round(sum(float(row.get("net_pnl") or 0) for row in completed), 2),
        "rows": rows,
        "valuation_basis": "next_available_session_close_after_T1_with_sell_costs",
    }


def actual_ledger_summary(ledger: Path, start_date: str, end_date: str) -> dict[str, Any]:
    if not ledger.exists():
        return {"status": "LEDGER_MISSING", "order_count": 0, "fill_count": 0}
    with sqlite3.connect(ledger) as conn:
        order_count = conn.execute(
            "select count(*) from orders where substr(created_at,1,10) between ? and ?",
            (start_date, end_date),
        ).fetchone()[0]
        fill_count = conn.execute(
            "select count(*) from fills where substr(created_at,1,10) between ? and ?",
            (start_date, end_date),
        ).fetchone()[0]
        accounts = [
            {"account_id": row[0], "initial_cash": row[1], "cash": row[2], "updated_at": row[3]}
            for row in conn.execute("select account_id,initial_cash,cash,updated_at from accounts order by account_id")
        ]
    return {"status": "PASS", "order_count": order_count, "fill_count": fill_count, "accounts": accounts}


def markdown_report(payload: dict[str, Any]) -> str:
    if payload.get("status") != "PASS":
        return f"# 本地模拟盘周度复盘\n\n状态：{payload.get('status')}\n\n原因：{payload.get('error')}\n"
    actual = payload["actual_account"]
    same_day = payload["exact_research_same_day_mark"]
    t1 = payload["t1_next_close_observation"]
    lines = [
        "# 本地模拟盘周度复盘",
        "",
        f"- 区间：{payload['start_date']} 至 {payload['end_date']}（{payload['session_count']} 个交易日）",
        f"- 实际订单/成交：{actual['order_count']}/{actual['fill_count']}",
        f"- 有候选交易日：{payload['candidate_day_count']}/{payload['session_count']}",
        f"- 全池候选样本：{payload['candidate_sample_count']}",
        f"- 核心可执行/独立 probe：{payload['core_executable_count']}/{payload['pilot_plan_count']}",
        f"- 研究触发日：{payload['trade_activity']['research_trade_day_count']}/{payload['trade_activity']['session_count']}",
        f"- 精确研究子集当日收盘盯市：{same_day['filled_count']} 笔，{same_day['net_mark_pnl']:.2f} 元",
        f"- T+1 下一交易日收盘观察：{t1['completed_count']} 笔，{t1['net_pnl']:.2f} 元",
        f"- 告警：{', '.join(payload['alerts']) or '无'}",
        "",
        "| 日期 | 市场 | 候选 | 完整严格 | 核心 | probe | 研究触发 | 当日盯市 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["daily"]:
        lines.append(
            f"| {row['trade_date']} | {row['market_regime']} | {row['candidate_count']} | "
            f"{row['strict_full_gate_count']} | {row['core_executable_count']} | {row['pilot_count']} | "
            f"{row['research_filled_count']} | {row['research_net_mark_pnl']:.2f} |"
        )
    lines.extend(["", "说明：研究回放不写入模拟盘账本，最终交易日持仓仍按未实现观察处理。", ""])
    return "\n".join(lines)


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "session_count": payload.get("session_count", 0),
        "alerts": payload.get("alerts", []),
        "actual_fill_count": (payload.get("actual_account") or {}).get("fill_count", 0),
        "research_mark_pnl": (payload.get("exact_research_same_day_mark") or {}).get("net_mark_pnl", 0),
        "t1_next_close_pnl": (payload.get("t1_next_close_observation") or {}).get("net_pnl", 0),
    }


def count_json(path: Path) -> int:
    return len(list(path.glob("*.json"))) if path.exists() else 0


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CORE_PLAN_POLICY_ID = "local_sim_core_plan_v2"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.broker_adapter import (  # noqa: E402
    BrokerGuardCode,
    BrokerOrderRequest,
    BrokerSide,
    LocalSimBrokerAdapter,
    LocalSimBrokerConfig,
)
from scripts.export_local_sim_dashboard import build_payload  # noqa: E402
from scripts.push_local_sim_feishu import read_json, webhook_source_info, write_json  # noqa: E402

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local simulated broker readiness")
    parser.add_argument("--trade-date", default=tomorrow())
    parser.add_argument("--ledger", default="data/local_sim/broker.sqlite")
    parser.add_argument("--account-id", default="local-sim")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--dashboard-output", default="web_dashboard/data/local_sim/latest_account.json")
    parser.add_argument("--output", default="")
    parser.add_argument("--fix", action="store_true", help="Initialize the local account and export dashboard data")
    parser.add_argument("--allow-missing-feishu", action="store_true")
    parser.add_argument("--require-daily-loop", action="store_true")
    parser.add_argument("--require-buy-entry", action="store_true")
    args = parser.parse_args()

    payload = build_readiness(args)
    output = Path(args.output) if args.output else ROOT / "reports" / "local_sim_readiness" / args.trade_date / "readiness.json"
    write_json(output, payload)
    print(json.dumps(summary(payload), ensure_ascii=False, sort_keys=True), flush=True)
    if args.require_buy_entry:
        ready = payload["local_sim_buy_entry_ready"]
    elif args.require_daily_loop:
        ready = payload["local_sim_risk_loop_ready"]
    else:
        ready = payload["manual_local_sim_ready"]
    return 0 if ready else 1


def build_readiness(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id=args.account_id,
            initial_cash=args.initial_cash,
            ledger_path=args.ledger,
        )
    )
    if args.fix:
        adapter.initialize_account()
    account_ok, account_details = account_check(adapter, expected_cash=args.initial_cash)
    checks.append(check("local_account", account_ok, account_details))

    dashboard_path = ROOT / args.dashboard_output if not Path(args.dashboard_output).is_absolute() else Path(args.dashboard_output)
    if args.fix and account_ok:
        existing_dashboard = read_json(dashboard_path, {})
        existing_is_complete = (
            isinstance(existing_dashboard, dict)
            and existing_dashboard.get("trade_date") == args.trade_date
            and existing_dashboard.get("valuation_complete") is True
        )
        if not existing_is_complete:
            dashboard_payload = build_payload(Path(args.ledger), args.account_id, args.trade_date)
            write_json(dashboard_path, dashboard_payload)
    dashboard_ok = dashboard_path.exists()
    checks.append(check("dashboard_data", dashboard_ok, {"path": str(dashboard_path)}))

    feishu = webhook_source_info()
    feishu_ok = bool(feishu.get("configured")) or bool(args.allow_missing_feishu)
    checks.append(check("feishu_webhook", feishu_ok, {"configured": bool(feishu.get("configured")), "source": feishu.get("source"), "fingerprint": feishu.get("fingerprint")}))

    t1_ok, t1_details = local_t1_gate_check(args.trade_date)
    checks.append(check("local_t_plus_one_gate", t1_ok, t1_details))

    session_date_ok, session_date_details = session_date_required_check(args.trade_date)
    checks.append(check("session_date_required_gate", session_date_ok, session_date_details))

    shadow_ok, shadow_details = shadow_readiness_check()
    checks.append(check("shadow_readiness_false", shadow_ok, shadow_details))

    python_ok = sys.version_info >= (3, 11)
    checks.append(check("python_3_11_or_newer", python_ok, {"version": sys.version.split()[0]}))

    manual_blocking = [item for item in checks if not item["passed"]]
    manual_ready = not manual_blocking
    plan_ok, plan_details = core_plan_check(Path(args.ledger), args.account_id, args.trade_date)
    checks.append(check("daily_core_plan", plan_ok, plan_details))
    executor_paths = [
        ROOT / "scripts" / "execute_local_sim_triggers.py",
        ROOT / "scripts" / "execute_local_sim_risk_exits.py",
        ROOT / "scripts" / "local_sim_risk_scan.py",
    ]
    executors_ok = all(path.exists() for path in executor_paths)
    checks.append(check("local_execution_scripts", executors_ok, {"paths": [str(path) for path in executor_paths]}))
    local_bridge_ready = manual_ready and executors_ok
    risk_loop_ready = local_bridge_ready
    buy_entry_ready = risk_loop_ready and plan_ok
    daily_loop_blocking = [item for item in checks if not item["passed"]]
    risk_blocking = [item for item in manual_blocking]
    if not executors_ok:
        risk_blocking.append(next(item for item in checks if item["name"] == "local_execution_scripts"))
    return {
        "schema_version": "chan520_local_sim_readiness_v0",
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "trade_date": args.trade_date,
        "manual_local_sim_ready": manual_ready,
        "local_sim_open_close_bridge_ready": local_bridge_ready,
        "local_sim_risk_loop_ready": risk_loop_ready,
        "local_sim_buy_entry_ready": buy_entry_ready,
        "local_sim_daily_loop_ready": risk_loop_ready,
        "auto_open_close_kernel_ready": False,
        "gm_adapter_shadow_ready": False,
        "shadow_readiness": False,
        "status": (
            "PASS_LOCAL_SIM_BUY_ENTRY_READY"
            if buy_entry_ready
            else "PASS_LOCAL_SIM_RISK_LOOP_ONLY"
            if risk_loop_ready
            else "PASS_MANUAL_ONLY"
            if manual_ready
            else "FAIL_CLOSED"
        ),
        "manual_blocking_checks": [item["name"] for item in manual_blocking],
        "risk_blocking_checks": [item["name"] for item in risk_blocking],
        "buy_entry_blocking_checks": [item["name"] for item in daily_loop_blocking],
        "blocking_checks": [item["name"] for item in daily_loop_blocking],
        "checks": checks,
        "notes": [
            "本检查确认本地 SQLite 模拟盘与 paper open/close bridge 的运行前置条件；仅限本地模拟盘。",
            "local_sim_risk_loop_ready 与 local_sim_buy_entry_ready 分离；核心计划失败只关闭新增买入，不关闭持仓风控。",
            "auto_open_close_kernel_ready 保持 false；日常本地执行器不等同于正式 paper kernel 或 GM adapter 端到端就绪。",
            "GM adapter/shadow 自动提交仍保持 fail-closed；shadow_readiness 必须保持 false。",
        ],
    }


def account_check(adapter: LocalSimBrokerAdapter, *, expected_cash: float) -> tuple[bool, dict[str, Any]]:
    try:
        snapshot = adapter.account_snapshot()
    except Exception as exc:  # noqa: BLE001 - readiness should report all local failures.
        return False, {"error": type(exc).__name__, "message": str(exc)}
    cash = float(snapshot.get("cash", 0.0))
    initial_cash = float(snapshot.get("initial_cash", 0.0))
    return (
        initial_cash == float(expected_cash),
        {
            "account_id": snapshot.get("account_id"),
            "initial_cash": initial_cash,
            "cash": cash,
            "position_count": len(snapshot.get("positions", [])),
            "expected_initial_cash": float(expected_cash),
        },
    )


def local_t1_gate_check(trade_date: str) -> tuple[bool, dict[str, Any]]:
    ledger = Path("/tmp") / f"chan520_t1_gate_{trade_date}.sqlite"
    adapter = LocalSimBrokerAdapter(LocalSimBrokerConfig(account_id="readiness-t1", ledger_path=str(ledger)))
    buy = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id=f"readiness-buy-{trade_date}",
            session_date=trade_date,
        )
    )
    sell = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.SELL,
            volume=100,
            price=10.0,
            client_order_id=f"readiness-sell-{trade_date}",
            session_date=trade_date,
        )
    )
    ok = buy.reason_code is BrokerGuardCode.OK and sell.reason_code is BrokerGuardCode.T_PLUS_ONE_BLOCKED
    return ok, {"buy_reason_code": buy.reason_code.value, "same_day_sell_reason_code": sell.reason_code.value}


def session_date_required_check(trade_date: str) -> tuple[bool, dict[str, Any]]:
    ledger = Path("/tmp") / f"chan520_session_gate_{trade_date}.sqlite"
    adapter = LocalSimBrokerAdapter(LocalSimBrokerConfig(account_id="readiness-date", ledger_path=str(ledger)))
    result = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id=f"readiness-missing-date-{trade_date}",
        )
    )
    ok = result.reason_code is BrokerGuardCode.SESSION_DATE_REQUIRED
    return ok, {"reason_code": result.reason_code.value, "message": result.message}


def shadow_readiness_check() -> tuple[bool, dict[str, Any]]:
    paths = sorted(ROOT.glob("reports/**/shadow_readiness*.json"))
    true_paths = []
    unreadable = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - report unreadable evidence.
            unreadable.append({"path": str(path), "error": type(exc).__name__})
            continue
        if bool(payload.get("shadow_readiness")):
            true_paths.append(str(path))
    return not true_paths and not unreadable, {"files_checked": len(paths), "true_paths": true_paths, "unreadable": unreadable}


def core_plan_check(ledger: Path, account_id: str, trade_date: str) -> tuple[bool, dict[str, Any]]:
    if not ledger.exists():
        return False, {"reason": "ledger_missing"}
    try:
        with sqlite3.connect(ledger) as conn:
            rows = conn.execute(
                "select status, payload_json from planned_orders where account_id = ? and trade_date = ? and upper(side) = 'BUY'",
                (account_id, trade_date),
            ).fetchall()
    except sqlite3.Error as exc:
        return False, {"reason": "ledger_error", "message": str(exc)}
    approved_policy = 0
    executable_approved = 0
    for status, payload_json in rows:
        try:
            payload = json.loads(str(payload_json or "{}"))
        except json.JSONDecodeError:
            continue
        if payload.get("local_sim_execution_policy_id") != CORE_PLAN_POLICY_ID:
            continue
        approved_policy += 1
        if status in {"WATCH_TRIGGER", "CONFIRMED_TRIGGER", "FILLED"}:
            executable_approved += 1
    report_path = ROOT / "reports" / "local_sim_plan" / trade_date.replace("-", "") / "core_plan.json"
    report = {}
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
    quality = report.get("scan_quality") if isinstance(report.get("scan_quality"), dict) else {}
    report_ok = (
        report.get("status") == "PASS"
        and report.get("policy_id") == CORE_PLAN_POLICY_ID
        and quality.get("coverage_pass") is True
        and quality.get("execution_coverage_pass") is True
        and int(report.get("executable_buy_count") or 0) > 0
    )
    return executable_approved > 0 and report_ok, {
        "plan_count": len(rows),
        "approved_policy_count": approved_policy,
        "executable_approved_count": executable_approved,
        "policy_id": CORE_PLAN_POLICY_ID,
        "report_path": str(report_path),
        "report_status": report.get("status"),
        "coverage": quality.get("coverage"),
        "coverage_pass": quality.get("coverage_pass"),
        "execution_coverage": quality.get("execution_coverage"),
        "execution_coverage_pass": quality.get("execution_coverage_pass"),
    }


def check(name: str, passed: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "details": details}


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": payload.get("trade_date"),
        "status": payload.get("status"),
        "manual_local_sim_ready": payload.get("manual_local_sim_ready"),
        "local_sim_open_close_bridge_ready": payload.get("local_sim_open_close_bridge_ready"),
        "local_sim_risk_loop_ready": payload.get("local_sim_risk_loop_ready"),
        "local_sim_buy_entry_ready": payload.get("local_sim_buy_entry_ready"),
        "local_sim_daily_loop_ready": payload.get("local_sim_daily_loop_ready"),
        "auto_open_close_kernel_ready": payload.get("auto_open_close_kernel_ready"),
        "gm_adapter_shadow_ready": payload.get("gm_adapter_shadow_ready"),
        "blocking_checks": payload.get("blocking_checks"),
    }


def tomorrow() -> str:
    return (datetime.now(SHANGHAI_TZ).date() + timedelta(days=1)).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())

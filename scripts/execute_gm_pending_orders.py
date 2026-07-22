from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
from datetime import date
from pathlib import Path
from queue import Queue
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.live_execution import (
    LiveExecutionBlocked,
    build_pending_execution_plan,
    execute_pending_plan,
    load_local_gm_sim_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute guarded GM simulation pending orders")
    parser.add_argument("--alpha-store", default=str(ROOT / "data" / "gm_alpha" / "chan520_alpha.sqlite"))
    parser.add_argument("--paper-store", default=str(ROOT / "data" / "paper" / "shadow_ranked_v1.sqlite"))
    parser.add_argument("--config", default=str(ROOT / "config" / "local_gm_sim.json"))
    parser.add_argument("--run-id", default="shadow_ranked_v1_2026")
    parser.add_argument("--execution-date", required=True)
    parser.add_argument("--decision-date", required=True)
    parser.add_argument("--output-root", default=str(ROOT / "reports" / "gm_sim_execution"))
    parser.add_argument("--confirm-submit", action="store_true")
    parser.add_argument("--account-query-timeout", type=int, default=15)
    args = parser.parse_args()

    output_dir = Path(args.output_root) / args.execution_date.replace("-", "")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"execute_pending_{args.execution_date}_{args.decision_date}.json"

    report: dict[str, Any] = {
        "execution_date": args.execution_date,
        "decision_date": args.decision_date,
        "confirm_submit": bool(args.confirm_submit),
        "status": "STARTED",
        "submitted_count": 0,
    }
    try:
        config = load_local_gm_sim_config(Path(args.config))
        report["config"] = {
            "account_id_present": bool(config.account_id),
            "account_type": config.account_type,
            "dry_run": config.dry_run,
            "enable_submit": config.enable_submit,
            "token_env": config.token_env,
            "token_present": bool(os.environ.get(config.token_env, "").strip()),
        }
        plan = build_pending_execution_plan(
            alpha_store=Path(args.alpha_store),
            paper_store=Path(args.paper_store),
            run_id=args.run_id,
            execution_date=date.fromisoformat(args.execution_date),
            decision_date=date.fromisoformat(args.decision_date),
        )
        report["plan"] = {
            "pending_count": len(plan.pending_rows),
            "data_max_dates": plan.data_max_dates,
        }
        account_probe = query_gm_account(config.account_id, config.token_env, timeout_seconds=args.account_query_timeout)
        report["account_probe"] = account_probe
        if not account_probe.get("ok"):
            raise LiveExecutionBlocked(f"GM account query failed or timed out: {account_probe}")
        results = execute_pending_plan(
            plan=plan,
            config=config,
            confirm_submit=bool(args.confirm_submit),
            account_query_ok=True,
        )
        report["results"] = results
        report["submitted_count"] = sum(1 for item in results if item.get("result", {}).get("submitted"))
        report["status"] = "EXECUTED" if args.confirm_submit else "DRY_RUN"
        report["exit_code"] = 0
    except LiveExecutionBlocked as exc:
        report["status"] = "FAIL_CLOSED"
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        report["exit_code"] = 2
    except Exception as exc:
        report["status"] = "ERROR"
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        report["exit_code"] = 1
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path), "submitted_count": report.get("submitted_count", 0)}, ensure_ascii=False), flush=True)
    return int(report.get("exit_code", 1))


def query_gm_account(account_id: str, token_env: str, *, timeout_seconds: int) -> dict[str, Any]:
    queue: Queue[dict[str, Any]] = Queue(maxsize=1)
    thread = threading.Thread(target=_query_gm_account_worker, args=(queue, account_id, token_env), daemon=True)
    thread.start()
    thread.join(timeout=max(1, timeout_seconds))
    if thread.is_alive():
        return {"ok": False, "timeout": True, "timeout_seconds": timeout_seconds}
    try:
        return queue.get_nowait()
    except Exception:
        return {"ok": False, "empty_result": True}


def _query_gm_account_worker(queue: Queue[dict[str, Any]], account_id: str, token_env: str) -> None:
    try:
        from gm.api.basic import set_account_id, set_token
        from gm.api.query import get_cash, get_position

        token = os.environ.get(token_env, "").strip()
        if not token:
            queue.put({"ok": False, "reason": "TOKEN_REQUIRED"})
            return
        set_token(token)
        if account_id:
            set_account_id(account_id)
        cash = get_cash(account_id=account_id)
        positions = get_position(account_id=account_id)
        payload: dict[str, Any] = {
            "ok": True,
            "cash_type": type(cash).__name__,
            "position_type": type(positions).__name__,
            "position_count": len(positions) if hasattr(positions, "__len__") else 0,
        }
        if isinstance(cash, dict):
            payload["cash_keys"] = sorted(list(cash.keys()))[:30]
            for key in ("nav", "available", "cash", "balance", "market_value"):
                if key in cash:
                    try:
                        payload[f"cash_{key}"] = float(cash[key])
                    except Exception:
                        payload[f"cash_{key}"] = str(cash[key])
        queue.put(payload)
    except Exception as exc:
        queue.put({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:240]})


if __name__ == "__main__":
    raise SystemExit(main())

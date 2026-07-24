from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.execution_policy import (
    BEAR_PILOT_ACCOUNT_ID,
    BEAR_PILOT_MAX_EXPOSURE_PCT,
    BEAR_PILOT_MAX_FILLS,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PYTHON = ROOT / ".venv311" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run scheduled Chan520 local simulated trading workflow")
    parser.add_argument("--phase", choices=["plan", "preopen", "intraday", "eod"], required=True)
    parser.add_argument("--trade-date", default="auto")
    parser.add_argument("--signal-date", default="", help="Prior completed market session used by the plan phase")
    parser.add_argument("--offline-regime", action="store_true", help="Generate a fail-closed plan without fetching index data")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--max-age-minutes", type=int, default=5)
    parser.add_argument("--max-fills", type=int, default=2)
    parser.add_argument("--max-exposure-pct", type=float, default=0.15)
    parser.add_argument("--confirmation-max-minutes", type=int, default=20)
    parser.add_argument("--dry-run-triggers", action="store_true")
    parser.add_argument("--feishu", choices=["off", "dry-run", "send"], default="dry-run")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dedupe-window-seconds", type=int, default=90)
    parser.add_argument("--force-run", action="store_true")
    parser.add_argument("--skip-if-plan-ready", action="store_true", help="Skip a retry when today's PASS plan already exists")
    args = parser.parse_args()

    trade_date = resolve_trade_date(args.trade_date)
    now = datetime.now(SHANGHAI_TZ)
    run_dir = ROOT / "reports" / "local_sim_daily" / trade_date.replace("-", "")
    run_stamp = now.strftime("%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.phase == "plan" and args.skip_if_plan_ready:
        ready = ready_plan_payload(trade_date)
        if ready is not None:
            print(json.dumps(ready, ensure_ascii=False, sort_keys=True), flush=True)
            return 0
    guard, skip = acquire_run_guard(
        run_dir,
        args.phase,
        now,
        dedupe_window_seconds=args.dedupe_window_seconds,
        force=args.force_run,
    )
    if skip:
        print(json.dumps(skip, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    log_dir = run_dir / "logs" / args.phase / run_stamp
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        steps = build_steps(args, trade_date)
        results = []
        exit_code = 0
        for index, step_payload in enumerate(steps, start=1):
            result = run_step(step_payload, index, log_dir)
            results.append(result)
            if result["returncode"] != 0:
                exit_code = result["returncode"]
                if not args.continue_on_error and args.phase != "plan":
                    break

        copy_if_exists(ROOT / "web_dashboard" / "data" / "local_sim" / "latest_account.json", run_dir / f"{args.phase}_{run_stamp}_dashboard.json")
        copy_if_exists(ROOT / "web_dashboard" / "data" / "local_sim" / "latest_account.json", run_dir / f"{args.phase}_dashboard.json")
        run_summary = summarize(args, trade_date, now, results)
        write_json(run_dir / f"{args.phase}_{run_stamp}_summary.json", run_summary)
        write_json(run_dir / f"{args.phase}_summary.json", run_summary)
        write_json(run_dir / "latest_run.json", run_summary)
        print(json.dumps({"phase": args.phase, "trade_date": trade_date, "run_dir": str(run_dir), "exit_code": exit_code}, ensure_ascii=False, sort_keys=True), flush=True)
        return exit_code
    finally:
        release_run_guard(guard, run_dir, args.phase, now)


def build_steps(args: argparse.Namespace, trade_date: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if args.phase == "plan":
        plan_args = ["--trade-date", trade_date, "--refresh-scan-if-missing"]
        signal_date = str(getattr(args, "signal_date", "") or "")
        if signal_date:
            plan_args.extend(["--signal-date", signal_date])
        if bool(getattr(args, "offline_regime", False)):
            plan_args.append("--offline-regime")
        steps.append(
            step(
                "generate_core_plan",
                script("generate_local_sim_core_plan.py", *plan_args),
                timeout_seconds=600,
                sla_seconds=300,
            )
        )
        steps.append(export_pilot_dashboard_step(trade_date, args.initial_cash))
        steps.append(export_dashboard_step(trade_date, args.initial_cash))
        steps.append(feishu_step(args, trade_date, "plan"))
    elif args.phase == "preopen":
        steps.append(
            step(
                "readiness",
                script("check_local_sim_readiness.py", "--trade-date", trade_date, "--fix", "--require-daily-loop"),
            )
        )
        steps.append(
            step(
                "trigger_preopen_dry_run",
                script(
                    "execute_local_sim_triggers.py",
                    "--trade-date",
                    trade_date,
                    "--max-age-minutes",
                    str(args.max_age_minutes),
                    "--max-fills",
                    str(args.max_fills),
                    "--max-exposure-pct",
                    str(args.max_exposure_pct),
                    "--confirmation-max-minutes",
                    str(args.confirmation_max_minutes),
                ),
            )
        )
        steps.append(export_pilot_dashboard_step(trade_date, args.initial_cash))
        steps.append(pilot_preopen_dry_run_step(args, trade_date))
        steps.append(export_dashboard_step(trade_date, args.initial_cash))
        steps.append(feishu_step(args, trade_date, "review"))
    elif args.phase == "intraday":
        steps.append(export_dashboard_step(trade_date, args.initial_cash))
        risk_scan_cmd = script("local_sim_risk_scan.py", "--trade-date", trade_date)
        if args.dry_run_triggers:
            risk_scan_cmd.append("--dry-run")
        steps.append(step("risk_scan", risk_scan_cmd))
        risk_cmd = script(
            "execute_local_sim_risk_exits.py",
            "--trade-date",
            trade_date,
            "--max-age-minutes",
            str(args.max_age_minutes),
            "--confirmation-max-minutes",
            str(args.confirmation_max_minutes),
        )
        if not args.dry_run_triggers:
            risk_cmd.append("--submit")
        steps.append(step("execute_risk_exits", risk_cmd))
        trigger_cmd = script(
            "execute_local_sim_triggers.py",
            "--trade-date",
            trade_date,
            "--max-age-minutes",
            str(args.max_age_minutes),
            "--max-fills",
            str(args.max_fills),
            "--max-exposure-pct",
            str(args.max_exposure_pct),
            "--confirmation-max-minutes",
            str(args.confirmation_max_minutes),
        )
        if not args.dry_run_triggers:
            trigger_cmd.append("--submit")
        steps.append(step("execute_triggers", trigger_cmd))
        steps.extend(pilot_intraday_steps(args, trade_date))
        steps.append(export_dashboard_step(trade_date, args.initial_cash))
        steps.append(feishu_step(args, trade_date, "trades"))
    else:
        steps.append(
            step(
                "refresh_local_market_store",
                script(
                    "refresh_local_market_store.py",
                    "--trade-date",
                    trade_date,
                    "--scan-if-missing",
                    "--rescan-unqualified",
                ),
                timeout_seconds=3600,
                sla_seconds=2700,
            )
        )
        steps.append(export_dashboard_step(trade_date, args.initial_cash))
        steps.append(step("risk_scan", script("local_sim_risk_scan.py", "--trade-date", trade_date)))
        steps.append(
            step(
                "replay_watch_only",
                script(
                    "replay_local_sim_watch_only.py",
                    "--trade-date",
                    trade_date,
                    "--initial-equity",
                    str(args.initial_cash),
                    "--max-fills",
                    str(args.max_fills),
                    "--max-exposure-pct",
                    str(min(args.max_exposure_pct, BEAR_PILOT_MAX_EXPOSURE_PCT)),
                ),
            )
        )
        steps.append(export_pilot_dashboard_step(trade_date, args.initial_cash))
        steps.append(pilot_risk_scan_step(args, trade_date))
        steps.append(export_dashboard_step(trade_date, args.initial_cash))
        steps.append(feishu_step(args, trade_date, "review"))
    return [item for item in steps if item]


def script(name: str, *args: str) -> list[str]:
    return [str(PYTHON), str(ROOT / "scripts" / name), *args]


def export_dashboard_step(trade_date: str, initial_cash: float) -> dict[str, Any]:
    return step(
        "export_dashboard",
        script(
            "export_local_sim_dashboard.py",
            "--trade-date",
            trade_date,
            "--initial-cash",
            str(initial_cash),
            "--mark-quotes",
        ),
    )


def export_pilot_dashboard_step(trade_date: str, initial_cash: float) -> dict[str, Any]:
    return step(
        "export_bear_pilot_dashboard",
        script(
            "export_local_sim_dashboard.py",
            "--trade-date",
            trade_date,
            "--initial-cash",
            str(initial_cash),
            "--account-id",
            BEAR_PILOT_ACCOUNT_ID,
            "--output",
            str(ROOT / "web_dashboard" / "data" / "local_sim" / "latest_bear_pilot.json"),
            "--quote-cache",
            str(ROOT / "data" / "local_sim" / "bear_pilot_quote_cache.json"),
            "--risk-state",
            str(ROOT / "data" / "local_sim" / "bear_pilot_risk_state.json"),
            "--mark-quotes",
        ),
    )


def pilot_risk_scan_step(args: argparse.Namespace, trade_date: str) -> dict[str, Any]:
    cmd = script(
        "local_sim_risk_scan.py",
        "--trade-date",
        trade_date,
        "--account-id",
        BEAR_PILOT_ACCOUNT_ID,
        "--data",
        str(ROOT / "web_dashboard" / "data" / "local_sim" / "latest_bear_pilot.json"),
        "--state",
        str(ROOT / "data" / "local_sim" / "bear_pilot_risk_state.json"),
        "--output",
        str(ROOT / "reports" / "local_sim_risk" / trade_date / "bear_pilot_risk_scan.json"),
    )
    if bool(getattr(args, "dry_run_triggers", False)):
        cmd.append("--dry-run")
    return step("bear_pilot_risk_scan", cmd)


def pilot_preopen_dry_run_step(args: argparse.Namespace, trade_date: str) -> dict[str, Any]:
    return step(
        "bear_pilot_preopen_dry_run",
        script(
            "execute_local_sim_triggers.py",
            "--trade-date",
            trade_date,
            "--account-id",
            BEAR_PILOT_ACCOUNT_ID,
            "--max-age-minutes",
            str(args.max_age_minutes),
            "--max-fills",
            str(BEAR_PILOT_MAX_FILLS),
            "--max-exposure-pct",
            str(BEAR_PILOT_MAX_EXPOSURE_PCT),
            "--confirmation-max-minutes",
            str(args.confirmation_max_minutes),
            "--output",
            str(ROOT / "reports" / "local_sim_triggers" / trade_date.replace("-", "") / "bear_pilot_preopen.json"),
        ),
    )


def pilot_intraday_steps(args: argparse.Namespace, trade_date: str) -> list[dict[str, Any]]:
    risk_exit = script(
        "execute_local_sim_risk_exits.py",
        "--trade-date",
        trade_date,
        "--account-id",
        BEAR_PILOT_ACCOUNT_ID,
        "--max-age-minutes",
        str(args.max_age_minutes),
        "--confirmation-max-minutes",
        str(args.confirmation_max_minutes),
        "--output",
        str(ROOT / "reports" / "local_sim_risk_exit" / trade_date.replace("-", "") / "bear_pilot_latest.json"),
    )
    trigger = script(
        "execute_local_sim_triggers.py",
        "--trade-date",
        trade_date,
        "--account-id",
        BEAR_PILOT_ACCOUNT_ID,
        "--max-age-minutes",
        str(args.max_age_minutes),
        "--max-fills",
        str(BEAR_PILOT_MAX_FILLS),
        "--max-exposure-pct",
        str(BEAR_PILOT_MAX_EXPOSURE_PCT),
        "--confirmation-max-minutes",
        str(args.confirmation_max_minutes),
        "--output",
        str(ROOT / "reports" / "local_sim_triggers" / trade_date.replace("-", "") / "bear_pilot_latest.json"),
    )
    if not args.dry_run_triggers:
        risk_exit.append("--submit")
        trigger.append("--submit")
    return [
        export_pilot_dashboard_step(trade_date, args.initial_cash),
        pilot_risk_scan_step(args, trade_date),
        step("execute_bear_pilot_risk_exits", risk_exit),
        step("execute_bear_pilot_triggers", trigger),
        export_pilot_dashboard_step(trade_date, args.initial_cash),
    ]


def feishu_step(args: argparse.Namespace, trade_date: str, mode: str) -> dict[str, Any] | None:
    if args.feishu == "off":
        return None
    cmd = script("push_local_sim_feishu.py", "--mode", mode, "--trade-date", trade_date)
    if args.feishu == "dry-run":
        cmd.append("--dry-run")
    return step(f"feishu_{mode}", cmd)


def step(
    name: str,
    cmd: list[str],
    *,
    timeout_seconds: int = 1800,
    sla_seconds: int | None = None,
) -> dict[str, Any]:
    return {"name": name, "cmd": cmd, "timeout_seconds": timeout_seconds, "sla_seconds": sla_seconds}


def run_step(step_payload: dict[str, Any], index: int, log_dir: Path) -> dict[str, Any]:
    name = str(step_payload["name"])
    started = datetime.now(SHANGHAI_TZ)
    proc = subprocess.Popen(
        step_payload["cmd"],
        cwd=ROOT,
        env=clean_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        timeout_seconds = int(step_payload.get("timeout_seconds") or 1800)
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        # Kill the whole step process group: network clients can leave child
        # processes holding the capture pipes open after the parent is killed.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        stdout = stdout or exc.stdout or ""
        stderr = stderr or exc.stderr or ""
        stderr += f"\nstep timed out after {timeout_seconds} seconds; process group terminated\n"
        returncode = 124
    ended = datetime.now(SHANGHAI_TZ)
    prefix = f"{index:02d}_{safe_name(name)}"
    stdout_path = log_dir / f"{prefix}.stdout.log"
    stderr_path = log_dir / f"{prefix}.stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    duration = (ended - started).total_seconds()
    sla_seconds = step_payload.get("sla_seconds")
    return {
        "name": name,
        "cmd": redact_cmd(step_payload["cmd"]),
        "returncode": returncode,
        "started_at": started.isoformat(timespec="seconds"),
        "ended_at": ended.isoformat(timespec="seconds"),
        "duration_seconds": round(duration, 3),
        "timeout_seconds": int(step_payload.get("timeout_seconds") or 1800),
        "sla_seconds": sla_seconds,
        "sla_pass": sla_seconds is None or (returncode == 0 and duration <= float(sla_seconds)),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


def summarize(args: argparse.Namespace, trade_date: str, started_at: datetime, results: list[dict[str, Any]]) -> dict[str, Any]:
    sla_steps = [item for item in results if item.get("sla_seconds") is not None]
    return {
        "schema_version": "chan520_local_sim_daily_run_v0",
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "started_at": started_at.isoformat(timespec="seconds"),
        "phase": args.phase,
        "trade_date": trade_date,
        "feishu": args.feishu,
        "dry_run_triggers": bool(args.dry_run_triggers),
        "status": "PASS" if all(item["returncode"] == 0 for item in results) else "FAIL",
        "operational_sla": {
            "status": "PASS" if all(item.get("sla_pass") for item in sla_steps) else "FAIL",
            "measured_step_count": len(sla_steps),
            "failed_steps": [item["name"] for item in sla_steps if not item.get("sla_pass")],
        },
        "steps": results,
    }


def ready_plan_payload(trade_date: str) -> dict[str, Any] | None:
    path = ROOT / "reports" / "local_sim_plan" / trade_date.replace("-", "") / "core_plan.json"
    payload = read_json(path, {})
    if payload.get("trade_date") != trade_date or payload.get("status") != "PASS":
        return None
    generated = parse_iso_datetime(payload.get("generated_at"))
    if generated is None or generated.astimezone(SHANGHAI_TZ).date().isoformat() != trade_date:
        return None
    return {
        "phase": "plan",
        "trade_date": trade_date,
        "status": "SKIPPED_PLAN_READY",
        "reason": "CURRENT_PASS_PLAN_ALREADY_EXISTS",
        "plan_path": str(path),
        "generated_at": payload.get("generated_at"),
    }


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(key, None)
    return env


def resolve_trade_date(value: str) -> str:
    if value == "auto":
        return date.today().isoformat()
    date.fromisoformat(value)
    return value


def copy_if_exists(src: Path, dst: Path) -> None:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def acquire_run_guard(
    run_dir: Path,
    phase: str,
    started_at: datetime,
    *,
    dedupe_window_seconds: int,
    force: bool,
) -> tuple[Any | None, dict[str, Any] | None]:
    lock_path = run_dir / f".{safe_name(phase)}.lock"
    state_path = run_dir / f".{safe_name(phase)}_run_guard.json"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None, duplicate_skip_payload(phase, started_at, "RUN_ALREADY_ACTIVE")
    state = read_json(state_path, {})
    previous = parse_iso_datetime(state.get("started_at"))
    if not force and previous is not None:
        age = (started_at - previous).total_seconds()
        if 0 <= age < max(dedupe_window_seconds, 0):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            return None, duplicate_skip_payload(phase, started_at, f"RECENT_RUN_{age:.1f}s")
    write_json(
        state_path,
        {
            "phase": phase,
            "started_at": started_at.isoformat(timespec="seconds"),
            "status": "RUNNING",
            "pid": os.getpid(),
        },
    )
    return handle, None


def release_run_guard(handle: Any | None, run_dir: Path, phase: str, started_at: datetime) -> None:
    if handle is None:
        return
    write_json(
        run_dir / f".{safe_name(phase)}_run_guard.json",
        {
            "phase": phase,
            "started_at": started_at.isoformat(timespec="seconds"),
            "completed_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "status": "COMPLETED",
            "pid": os.getpid(),
        },
    )
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def duplicate_skip_payload(phase: str, started_at: datetime, reason: str) -> dict[str, Any]:
    return {
        "phase": phase,
        "generated_at": started_at.isoformat(timespec="seconds"),
        "status": "SKIPPED_DUPLICATE",
        "reason": reason,
    }


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def parse_iso_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=SHANGHAI_TZ)


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:80]


def redact_cmd(cmd: list[str]) -> list[str]:
    return [item if "hook/" not in item and "token" not in item.lower() else "<redacted>" for item in cmd]


if __name__ == "__main__":
    raise SystemExit(main())

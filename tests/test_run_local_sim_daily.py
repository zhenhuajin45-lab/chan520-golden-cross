from __future__ import annotations

from argparse import Namespace
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.run_local_sim_daily import acquire_run_guard, build_steps, release_run_guard


TZ = ZoneInfo("Asia/Shanghai")


def args(**overrides):
    payload = {
        "phase": "preopen",
        "initial_cash": 1_000_000.0,
        "max_age_minutes": 5,
        "max_fills": 2,
        "max_exposure_pct": 0.15,
        "confirmation_max_minutes": 20,
        "dry_run_triggers": False,
        "feishu": "dry-run",
    }
    payload.update(overrides)
    return Namespace(**payload)


def test_preopen_uses_dry_run_trigger_without_submit():
    steps = build_steps(args(phase="preopen"), "2026-07-15")

    trigger = next(item for item in steps if item["name"] == "trigger_preopen_dry_run")
    assert "--submit" not in trigger["cmd"]
    assert any(item["name"] == "readiness" for item in steps)
    pilot = next(item for item in steps if item["name"] == "bear_pilot_preopen_dry_run")
    assert "--submit" not in pilot["cmd"]


def test_intraday_submits_unless_explicit_dry_run():
    steps = build_steps(args(phase="intraday"), "2026-07-15")
    trigger = next(item for item in steps if item["name"] == "execute_triggers")

    assert "--submit" in trigger["cmd"]


def test_eod_records_review_not_trade_push():
    steps = build_steps(args(phase="eod", feishu="send"), "2026-07-15")

    assert any(item["name"] == "risk_scan" for item in steps)
    assert any(item["name"] == "replay_watch_only" for item in steps)
    assert any(item["name"] == "feishu_review" for item in steps)
    assert not any(item["name"] == "feishu_trades" for item in steps)
    names = [item["name"] for item in steps]
    assert names.index("risk_scan") < names.index("replay_watch_only") < names.index("feishu_review")


def test_plan_phase_generates_core_plan_before_dashboard_and_feishu():
    steps = build_steps(args(phase="plan", feishu="send"), "2026-07-15")

    assert [item["name"] for item in steps] == [
        "generate_core_plan",
        "export_bear_pilot_dashboard",
        "export_dashboard",
        "feishu_plan",
    ]


def test_intraday_executes_risk_exits_before_buy_triggers():
    steps = build_steps(args(phase="intraday"), "2026-07-15")
    names = [item["name"] for item in steps]

    assert names.index("execute_risk_exits") < names.index("execute_triggers")
    assert names.index("bear_pilot_risk_scan") < names.index("execute_bear_pilot_risk_exits")
    assert names.index("execute_bear_pilot_risk_exits") < names.index("execute_bear_pilot_triggers")


def test_run_guard_skips_duplicate_without_overwriting_phase_summary(tmp_path):
    now = datetime(2026, 7, 17, 9, 36, 0, tzinfo=TZ)
    guard, skip = acquire_run_guard(tmp_path, "intraday", now, dedupe_window_seconds=90, force=False)
    assert guard is not None
    assert skip is None
    release_run_guard(guard, tmp_path, "intraday", now)

    guard, skip = acquire_run_guard(
        tmp_path,
        "intraday",
        datetime(2026, 7, 17, 9, 36, 30, tzinfo=TZ),
        dedupe_window_seconds=90,
        force=False,
    )
    assert guard is None
    assert skip["status"] == "SKIPPED_DUPLICATE"

from __future__ import annotations

import json
from types import SimpleNamespace

from chan520_skill.broker_adapter import LocalSimBrokerAdapter, LocalSimBrokerConfig
from chan520_skill.execution_policy import BEAR_PILOT_ACCOUNT_ID, BEAR_PILOT_POLICY_ID
from scripts import check_local_sim_readiness as readiness


def test_local_sim_readiness_passes_manual_mode_with_fix(tmp_path):
    args = SimpleNamespace(
        trade_date="2026-07-15",
        ledger=str(tmp_path / "local_sim.sqlite"),
        account_id="readiness-test",
        initial_cash=1_000_000.0,
        dashboard_output=str(tmp_path / "latest_account.json"),
        fix=True,
        allow_missing_feishu=True,
    )

    payload = readiness.build_readiness(args)

    assert payload["manual_local_sim_ready"] is True
    assert payload["local_sim_open_close_bridge_ready"] is True
    assert payload["local_sim_risk_loop_ready"] is True
    assert payload["local_sim_buy_entry_ready"] is False
    assert payload["local_sim_research_entry_ready"] is False
    assert payload["local_sim_any_entry_ready"] is False
    assert payload["local_sim_daily_loop_ready"] is True
    assert payload["auto_open_close_kernel_ready"] is False
    assert payload["gm_adapter_shadow_ready"] is False
    assert payload["shadow_readiness"] is False
    assert payload["manual_blocking_checks"] == []
    assert payload["risk_blocking_checks"] == []
    assert payload["buy_entry_blocking_checks"] == ["daily_core_plan"]
    assert payload["buy_entry_blocking_reasons"] == ["PLAN_REPORT_MISSING"]
    assert payload["blocking_checks"] == ["daily_core_plan"]
    assert payload["status"] == "PASS_LOCAL_SIM_RISK_LOOP_ONLY"
    assert (tmp_path / "latest_account.json").exists()


def test_research_plan_readiness_reports_armed_bear_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness, "ROOT", tmp_path)
    ledger = tmp_path / "broker.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id=BEAR_PILOT_ACCOUNT_ID, ledger_path=str(ledger))
    )
    adapter.record_planned_order(
        {
            "planned_order_id": "BEAR-PROBE:2026-08-03:600001",
            "trade_date": "2026-08-03",
            "symbol": "600001",
            "side": "BUY",
            "volume": 100,
            "status": "WATCH_TRIGGER",
            "local_sim_execution_policy_id": BEAR_PILOT_POLICY_ID,
        }
    )
    report_path = tmp_path / "reports" / "local_sim_plan" / "20260803" / "core_plan.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "market_regime": {"state": "BEAR"},
                "research_cohorts": {
                    "bear_pilot": {"status": "ARMED", "queued_count": 1},
                },
            }
        ),
        encoding="utf-8",
    )

    ok, details = readiness.research_plan_check(ledger, "2026-08-03")

    assert ok is True
    assert details["required"] is True
    assert details["executable_approved_count"] == 1
    assert details["policy_ids"] == [BEAR_PILOT_POLICY_ID]

from __future__ import annotations

from types import SimpleNamespace

from scripts.check_local_sim_readiness import build_readiness


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

    payload = build_readiness(args)

    assert payload["manual_local_sim_ready"] is True
    assert payload["local_sim_open_close_bridge_ready"] is True
    assert payload["local_sim_risk_loop_ready"] is True
    assert payload["local_sim_buy_entry_ready"] is False
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

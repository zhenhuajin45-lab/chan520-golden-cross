from __future__ import annotations

import json
from pathlib import Path

from chan520_skill.broker_adapter import BrokerOrderRequest, BrokerSide, LocalSimBrokerAdapter, LocalSimBrokerConfig
from chan520_skill.execution_policy import BEAR_PILOT_ACCOUNT_ID, CORE_ACCOUNT_ID
from scripts import export_local_sim_dashboard as exporter
from scripts.export_local_sim_dashboard import build_payload


def test_export_includes_planned_orders_and_quote_fallback(tmp_path):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(ledger),
        )
    )
    adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id="buy-1",
            session_date="2026-07-15",
            extra={"entry_reason": "趋势回踩计划入场"},
        )
    )
    adapter.record_planned_order(
        {
            "planned_order_id": "plan-1",
            "trade_date": "2026-07-15",
            "symbol": "SHSE.600288",
            "stock_name": "大恒科技",
            "side": "SELL",
            "volume": 100,
            "status": "PLANNED",
            "reason_text": "盘中失效价风控候选",
        }
    )
    adapter.record_planned_order(
        {
            "planned_order_id": "old-plan",
            "trade_date": "2026-07-14",
            "symbol": "SZSE.000001",
            "side": "BUY",
            "volume": 100,
            "status": "EXPIRED_PLAN_DATE",
        }
    )

    payload = build_payload(Path(ledger), "local-test", "2026-07-15")

    assert payload["positions"][0]["quote_status"] == "COST_FALLBACK"
    assert payload["positions"][0]["entry_reason"] == "趋势回踩计划入场"
    assert payload["positions"][0]["sellable_shares"] == 0
    assert payload["positions"][0]["t_plus_one_status"] == "T_PLUS_ONE_BLOCKED"
    assert payload["positions"][0]["profit_protection_armed"] is False
    assert payload["valuation_complete"] is False
    assert payload["valuation_status"] == "DEGRADED"
    assert round(payload["account"]["total_pnl"], 2) == -5.01
    assert payload["planned_orders"][0]["planned_order_id"] == "plan-1"
    assert payload["planned_orders"][0]["stock_name"] == "大恒科技"
    assert payload["planned_orders"][0]["display_symbol"] == "SHSE.600288 大恒科技"
    assert payload["planned_orders"][0]["reason_text"] == "盘中失效价风控候选"
    assert len(payload["planned_orders"]) == 1


def test_export_uses_last_valid_quote_cache_and_reports_sellable_shares(tmp_path, monkeypatch):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="local-test", initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id="buy-cache",
            session_date="2026-07-15",
        )
    )
    cache = {"quotes": {}}
    monkeypatch.setattr(
        exporter,
        "tencent_quote",
        lambda _code: {
            "code": "600288",
            "name": "大恒科技",
            "price": "9.50",
            "prev_close": "10.00",
            "datetime": "20260715150000",
        },
    )

    live = build_payload(Path(ledger), "local-test", "2026-07-15", mark_quotes=True, quote_cache=cache)

    assert live["valuation_complete"] is True
    assert live["positions"][0]["market_price"] == 9.5
    assert cache["quotes"]["SHSE.600288"]["market_price"] == 9.5

    def quote_failure(_code):
        raise exporter.DataError("network unavailable")

    monkeypatch.setattr(exporter, "tencent_quote", quote_failure)
    cached = build_payload(Path(ledger), "local-test", "2026-07-16", mark_quotes=True, quote_cache=cache)

    assert cached["valuation_status"] == "STALE"
    assert cached["valuation_complete"] is True
    assert cached["positions"][0]["quote_status"] == "STALE_CACHE"
    assert cached["positions"][0]["market_price"] == 9.5
    assert cached["positions"][0]["sellable_shares"] == 100
    assert cached["positions"][0]["t_plus_one_status"] == "SELLABLE"


def test_export_exposes_profit_high_water_state(tmp_path):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="local-test", initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id="buy-risk-state",
            session_date="2026-07-15",
        )
    )

    payload = build_payload(
        Path(ledger),
        "local-test",
        "2026-07-16",
        risk_state={
            "positions": {
                "SHSE.600288": {
                    "peak_unrealized_pnl_pct": 0.04,
                    "intraday_high_pnl_pct": 0.04,
                    "updated_at": "2026-07-16T10:00:00+08:00",
                }
            }
        },
    )

    assert payload["positions"][0]["peak_unrealized_pnl_pct"] == 0.04
    assert payload["positions"][0]["profit_protection_armed"] is True


def test_missing_core_plan_is_reported_as_generation_failure(tmp_path, monkeypatch):
    run_dir = tmp_path / "reports" / "local_sim_daily" / "20260722"
    run_dir.mkdir(parents=True)
    (run_dir / "plan_summary.json").write_text(
        """{
          "status": "FAIL",
          "steps": [{
            "name": "generate_core_plan",
            "returncode": 1,
            "stderr_tail": "Eastmoney 502"
          }]
        }""",
        encoding="utf-8",
    )
    monkeypatch.setattr(exporter, "ROOT", tmp_path)

    core = exporter.load_core_plan("2026-07-22")

    assert core["status"] == "GENERATION_FAILED"
    assert core["executable_buy_count"] == 0
    assert core["failure_step"] == "generate_core_plan"
    assert "502" in core["failure_reason"]


def test_core_dashboard_embeds_isolated_bear_pilot_account(tmp_path):
    ledger = tmp_path / "local_sim.sqlite"
    core = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id=CORE_ACCOUNT_ID, initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    core.initialize_account()
    pilot = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id=BEAR_PILOT_ACCOUNT_ID, initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    pilot.record_planned_order(
        {
            "planned_order_id": "BEAR-PILOT:2026-07-22:600671",
            "trade_date": "2026-07-22",
            "symbol": "600671",
            "stock_name": "天目药业",
            "side": "BUY",
            "volume": 2500,
            "status": "WATCH_TRIGGER",
            "reason_text": "熊市防御研究小仓",
        }
    )

    payload = build_payload(Path(ledger), CORE_ACCOUNT_ID, "2026-07-22")

    research = payload["research_pilot"]
    assert research["status"] == "ACTIVE"
    assert research["account"]["account_id"] == BEAR_PILOT_ACCOUNT_ID
    assert research["core_account_affected"] is False
    assert research["gm_submit_enabled"] is False
    assert research["planned_orders"][0]["stock_name"] == "天目药业"


def test_counterfactual_export_includes_sampling_and_full_pool_metrics(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports" / "local_sim_counterfactual" / "20260723"
    report_dir.mkdir(parents=True)
    (report_dir / "watch_only_replay.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "position_cap_pct": 0.025,
                "max_exposure_pct": 0.05,
                "sampling_interval_minutes": 2,
                "all_candidate_independent_results": [{"symbol": "600012"}],
                "all_candidate_ranked_portfolio": {
                    "candidate_count": 16,
                    "filled_count": 2,
                    "net_mark_pnl": -403.88,
                },
                "all_candidate_close_summary": {
                    "candidate_count": 21,
                    "mean_close_return_pct": -0.341039,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(exporter, "ROOT", tmp_path)

    replay = exporter.load_counterfactual_replay("2026-07-23")

    assert replay["position_cap_pct"] == 0.025
    assert replay["max_exposure_pct"] == 0.05
    assert replay["sampling_interval_minutes"] == 2
    assert replay["all_candidate_independent_results"][0]["symbol"] == "600012"
    assert replay["all_candidate_close_summary"]["candidate_count"] == 21
    assert replay["all_candidate_ranked_portfolio"]["net_mark_pnl"] == -403.88


def test_session_market_snapshot_uses_completed_trade_date_refresh(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports" / "market_store" / "20260724"
    report_dir.mkdir(parents=True)
    (report_dir / "refresh.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "generated_at": "2026-07-24T15:38:15+08:00",
                "trade_date": "2026-07-24",
                "market_regime": {"state": "BEAR", "regime_ok": False, "detail": "close below MA20/MA60"},
                    "scan_quality": {
                        "universe": 4995,
                        "success": 4977,
                        "adjusted_success": 4959,
                        "history_source_counts": {
                            "tencent_qfq_plus_sina_exact": 4959,
                            "sina_unadjusted": 18,
                        },
                        "minimum_coverage": 0.85,
                    "minimum_execution_coverage": 0.85,
                },
                "scan_rows": 4977,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(exporter, "ROOT", tmp_path)

    snapshot = exporter.load_session_market_snapshot("2026-07-24")

    assert snapshot["market_regime"]["state"] == "BEAR"
    assert snapshot["scan_rows"] == 4977
    assert snapshot["scan_quality"]["research_coverage_pass"] is True
    assert snapshot["scan_quality"]["execution_coverage_pass"] is True

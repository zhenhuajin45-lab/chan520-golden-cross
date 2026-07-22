from __future__ import annotations

from pathlib import Path

from chan520_skill.broker_adapter import BrokerOrderRequest, BrokerSide, LocalSimBrokerAdapter, LocalSimBrokerConfig
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

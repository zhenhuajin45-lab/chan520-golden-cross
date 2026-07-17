from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chan520_skill.broker_adapter import LocalSimBrokerAdapter, LocalSimBrokerConfig
from scripts import execute_local_sim_triggers as triggers


TZ = ZoneInfo("Asia/Shanghai")


def make_adapter(tmp_path: Path) -> tuple[LocalSimBrokerAdapter, Path]:
    ledger = tmp_path / "broker.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(ledger),
        )
    )
    adapter.record_planned_order(
        {
            "planned_order_id": "WATCH-2026-07-15-000063",
            "order_intent_id": "PLAN-2026-07-15-V5-01",
            "trade_date": "2026-07-15",
            "symbol": "000063",
            "side": "BUY",
            "volume": 1000,
            "status": "WATCH_TRIGGER",
            "lower_price": 39.0,
            "upper_price": 40.5,
            "invalid_price": 41.0,
            "stop_price": 38.0,
            "trigger_price": 39.5,
            "ma5": 39.5,
            "ma20": 38.0,
            "market_regime": "NORMAL",
            "local_sim_execution_policy_id": triggers.CORE_PLAN_POLICY_ID,
            "reason_text": "520金叉计划入场",
        }
    )
    return adapter, ledger


def test_trigger_cycle_blocks_before_continuous_auction(tmp_path, monkeypatch):
    adapter, ledger = make_adapter(tmp_path)
    monkeypatch.setattr(
        triggers,
        "tencent_quote",
        lambda code: {
            "code": code,
            "name": "中兴通讯",
            "price": "39.8",
            "prev_close": "39.9",
            "datetime": "20260715092500",
        },
    )

    payload = triggers.run_trigger_cycle(
        adapter=adapter,
        ledger=ledger,
        account_id="local-test",
        trade_date="2026-07-15",
        now=datetime(2026, 7, 15, 9, 25, tzinfo=TZ),
        max_age_minutes=5,
        max_fills=2,
        max_exposure_pct=0.15,
        max_trigger_drawdown_pct=1.2,
        submit=True,
        ignore_time_gate=False,
        market_context=healthy_market_context(),
    )

    assert payload["results"][0]["reason"] == "NOT_IN_CONTINUOUS_AUCTION"
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("select count(*) from fills").fetchone()[0] == 0


def test_trigger_cycle_confirms_before_submitting(tmp_path, monkeypatch):
    adapter, ledger = make_adapter(tmp_path)
    monkeypatch.setattr(
        triggers,
        "tencent_quote",
        lambda code: {
            "code": code,
            "name": "中兴通讯",
            "price": "39.8",
            "prev_close": "39.9",
            "datetime": "20260715100100",
        },
    )

    payload = triggers.run_trigger_cycle(
        adapter=adapter,
        ledger=ledger,
        account_id="local-test",
        trade_date="2026-07-15",
        now=datetime(2026, 7, 15, 10, 2, tzinfo=TZ),
        max_age_minutes=5,
        max_fills=2,
        max_exposure_pct=0.15,
        max_trigger_drawdown_pct=1.2,
        submit=True,
        ignore_time_gate=False,
        market_context=healthy_market_context(),
    )

    row = payload["results"][0]
    assert row["reason"] == "TRIGGER_CONFIRMED"
    assert row["action"] == "CONFIRM"
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("select count(*) from fills").fetchone()[0] == 0
        assert conn.execute("select status from planned_orders").fetchone()[0] == "CONFIRMED_TRIGGER"

    payload = triggers.run_trigger_cycle(
        adapter=adapter,
        ledger=ledger,
        account_id="local-test",
        trade_date="2026-07-15",
        now=datetime(2026, 7, 15, 10, 5, tzinfo=TZ),
        max_age_minutes=5,
        max_fills=2,
        max_exposure_pct=0.15,
        max_trigger_drawdown_pct=1.2,
        submit=True,
        ignore_time_gate=False,
        market_context=healthy_market_context(),
    )

    row = payload["results"][0]
    assert row["reason"] == "TRIGGER_MATCHED"
    assert row["broker_result"]["accepted"] is True
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("select count(*) from fills").fetchone()[0] == 1
        assert conn.execute("select status from planned_orders").fetchone()[0] == "FILLED"


def test_trigger_cycle_blocks_weak_intraday_trigger(tmp_path, monkeypatch):
    adapter, ledger = make_adapter(tmp_path)
    monkeypatch.setattr(
        triggers,
        "tencent_quote",
        lambda code: {
            "code": code,
            "name": "中兴通讯",
            "price": "39.8",
            "prev_close": "40.6",
            "datetime": "20260715100100",
        },
    )

    payload = triggers.run_trigger_cycle(
        adapter=adapter,
        ledger=ledger,
        account_id="local-test",
        trade_date="2026-07-15",
        now=datetime(2026, 7, 15, 10, 2, tzinfo=TZ),
        max_age_minutes=5,
        max_fills=2,
        max_exposure_pct=0.15,
        max_trigger_drawdown_pct=1.2,
        submit=True,
        ignore_time_gate=False,
        market_context=healthy_market_context(),
    )

    assert payload["results"][0]["reason"] == "INTRADAY_WEAKNESS_BLOCKED"
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("select count(*) from fills").fetchone()[0] == 0


def test_trigger_cycle_with_no_executable_plans_does_not_require_live_marks(tmp_path, monkeypatch):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="local-test", initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    adapter.initialize_account()

    def unexpected_quote(_code):
        raise AssertionError("quotes must not be requested when there are no executable plans")

    monkeypatch.setattr(triggers, "tencent_quote", unexpected_quote)
    payload = triggers.run_trigger_cycle(
        adapter=adapter,
        ledger=ledger,
        account_id="local-test",
        trade_date="2026-07-16",
        now=datetime(2026, 7, 15, 18, 0, tzinfo=TZ),
        max_age_minutes=5,
        max_fills=2,
        max_exposure_pct=0.15,
        max_trigger_drawdown_pct=1.2,
        submit=False,
    )

    assert payload["plan_count"] == 0
    assert payload["blocking_errors"] == []
    assert payload["market_context"]["status"] == "NOT_REQUIRED"


def test_trigger_cycle_rejects_invalid_plan_geometry(tmp_path, monkeypatch):
    adapter, ledger = make_adapter(tmp_path)
    with sqlite3.connect(ledger) as conn:
        conn.execute(
            "update planned_orders set stop_price = 40.0 where planned_order_id = ?",
            ("WATCH-2026-07-15-000063",),
        )
    monkeypatch.setattr(
        triggers,
        "tencent_quote",
        lambda code: {
            "code": code,
            "name": "中兴通讯",
            "price": "39.8",
            "prev_close": "39.9",
            "open": "39.8",
            "datetime": "20260715100100",
        },
    )

    payload = triggers.run_trigger_cycle(
        adapter=adapter,
        ledger=ledger,
        account_id="local-test",
        trade_date="2026-07-15",
        now=datetime(2026, 7, 15, 10, 2, tzinfo=TZ),
        max_age_minutes=5,
        max_fills=2,
        max_exposure_pct=0.15,
        max_trigger_drawdown_pct=1.2,
        submit=True,
        market_context=healthy_market_context(),
    )

    assert payload["results"][0]["reason"] == "INVALID_PLAN_GEOMETRY"


def test_trigger_cycle_blocks_new_entries_while_risk_exit_is_pending(tmp_path, monkeypatch):
    adapter, ledger = make_adapter(tmp_path)
    adapter.record_planned_order(
        {
            "planned_order_id": "RISK:2026-07-15:000001",
            "trade_date": "2026-07-15",
            "symbol": "000001",
            "side": "SELL",
            "volume": 100,
            "status": "RISK_CONFIRMED",
        }
    )
    monkeypatch.setattr(triggers, "tencent_quote", lambda code: {
        "code": code,
        "name": "中兴通讯",
        "price": "39.8",
        "prev_close": "39.9",
        "open": "39.8",
        "datetime": "20260715100100",
    })

    payload = triggers.run_trigger_cycle(
        adapter=adapter,
        ledger=ledger,
        account_id="local-test",
        trade_date="2026-07-15",
        now=datetime(2026, 7, 15, 10, 2, tzinfo=TZ),
        max_age_minutes=5,
        max_fills=2,
        max_exposure_pct=0.15,
        max_trigger_drawdown_pct=1.2,
        submit=True,
        market_context=healthy_market_context(),
    )

    assert payload["active_risk_exit_count"] == 1
    assert payload["results"][0]["reason"] == "ACCOUNT_RISK_EXIT_PENDING"


def test_trigger_cycle_blocks_broad_market_shock(tmp_path, monkeypatch):
    adapter, ledger = make_adapter(tmp_path)
    monkeypatch.setattr(triggers, "tencent_quote", lambda code: {
        "code": code,
        "name": "中兴通讯",
        "price": "39.8",
        "prev_close": "39.9",
        "open": "39.8",
        "datetime": "20260715100100",
    })
    context = healthy_market_context()
    context["indices"]["399001"]["pct_chg"] = -2.0
    context["indices"]["399006"]["pct_chg"] = -3.0

    payload = triggers.run_trigger_cycle(
        adapter=adapter,
        ledger=ledger,
        account_id="local-test",
        trade_date="2026-07-15",
        now=datetime(2026, 7, 15, 10, 2, tzinfo=TZ),
        max_age_minutes=5,
        max_fills=2,
        max_exposure_pct=0.15,
        max_trigger_drawdown_pct=1.2,
        submit=True,
        market_context=context,
    )

    assert payload["results"][0]["reason"] == "BROAD_MARKET_SHOCK_BLOCKED"


def healthy_market_context():
    return {
        "status": "OK",
        "indices": {
            "000001": {"pct_chg": 0.1},
            "399001": {"pct_chg": 0.1},
            "399006": {"pct_chg": 0.1},
            "000688": {"pct_chg": 0.1},
        },
    }

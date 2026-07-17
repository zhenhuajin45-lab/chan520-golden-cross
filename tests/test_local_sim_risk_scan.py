from __future__ import annotations

from types import SimpleNamespace
import sqlite3

import pytest

from scripts.local_sim_risk_scan import scan_risk


def test_risk_scan_creates_hard_stop_candidate(tmp_path):
    payload = {
        "trade_date": "2026-07-15",
        "positions": [
            {
                "symbol": "SHSE.600288",
                "shares": 100,
                "market_price": 9.4,
                "unrealized_pnl_pct": -0.06,
                "ma5": 9.0,
                "ma20": 8.5,
            }
        ],
    }
    state = {"positions": {}}
    args = SimpleNamespace(
        trade_date="2026-07-15",
        ledger=str(tmp_path / "local_sim.sqlite"),
        account_id="risk-test",
        initial_cash=1_000_000.0,
        hard_stop_pct=0.055,
        profit_protection_min_pct=0.30,
        profit_giveback_pct=0.18,
        profit_retained_ratio=0.65,
        dry_run=False,
    )

    audit = scan_risk(payload, state, args)

    assert audit["candidates"][0]["reason_code"] == "hard_stop_loss"
    assert audit["candidates"][0]["side"] == "SELL"
    assert state["positions"]["SHSE.600288"]["peak_unrealized_pnl_pct"] == -0.06


def test_risk_scan_prioritizes_ma20_breakdown(tmp_path):
    payload = {
        "trade_date": "2026-07-15",
        "positions": [
            {
                "symbol": "688371",
                "shares": 1000,
                "market_price": 37.93,
                "unrealized_pnl_pct": -0.0956,
                "ma5": 41.64,
                "ma20": 38.45,
            }
        ],
    }
    state = {"positions": {}}
    args = SimpleNamespace(
        trade_date="2026-07-15",
        ledger=str(tmp_path / "local_sim.sqlite"),
        account_id="risk-test",
        initial_cash=1_000_000.0,
        hard_stop_pct=0.055,
        profit_protection_min_pct=0.30,
        profit_giveback_pct=0.18,
        profit_retained_ratio=0.65,
        dry_run=True,
    )

    audit = scan_risk(payload, state, args)

    assert audit["candidates"][0]["reason_code"] == "ma20_breakdown"
    assert "跌破 MA20" in audit["candidates"][0]["reason_text"]
    assert audit["candidates"][0]["reason_codes"] == ["ma20_breakdown", "ma5_failed_reclaim", "hard_stop_loss"]


def test_risk_scan_fails_closed_when_valuation_is_incomplete(tmp_path):
    args = SimpleNamespace(
        trade_date="2026-07-15",
        ledger=str(tmp_path / "local_sim.sqlite"),
        account_id="risk-test",
        initial_cash=1_000_000.0,
        hard_stop_pct=0.055,
        profit_protection_min_pct=0.30,
        profit_giveback_pct=0.18,
        profit_retained_ratio=0.65,
        dry_run=False,
    )

    audit = scan_risk(
        {"trade_date": "2026-07-15", "valuation_complete": False, "positions": []},
        {"positions": {}},
        args,
    )

    assert audit["status"] == "FAIL_CLOSED"
    assert audit["blocking_errors"] == ["VALUATION_INCOMPLETE"]
    assert audit["candidates"] == []


def test_risk_scan_supersedes_legacy_duplicate_risk_plan(tmp_path):
    ledger = tmp_path / "local_sim.sqlite"
    args = SimpleNamespace(
        trade_date="2026-07-15",
        ledger=str(ledger),
        account_id="risk-test",
        initial_cash=1_000_000.0,
        hard_stop_pct=0.055,
        profit_protection_min_pct=0.30,
        profit_giveback_pct=0.18,
        profit_retained_ratio=0.65,
        dry_run=False,
    )
    from chan520_skill.broker_adapter import LocalSimBrokerAdapter, LocalSimBrokerConfig

    adapter = LocalSimBrokerAdapter(LocalSimBrokerConfig(account_id="risk-test", ledger_path=str(ledger)))
    adapter.record_planned_order(
        {
            "planned_order_id": "RISK:2026-07-15:600288:hard_stop_loss",
            "trade_date": "2026-07-15",
            "symbol": "600288",
            "side": "SELL",
            "volume": 100,
            "status": "RISK_CANDIDATE",
        }
    )

    scan_risk(
        {
            "trade_date": "2026-07-15",
            "valuation_complete": True,
            "positions": [
                {
                    "symbol": "600288",
                    "shares": 100,
                    "market_price": 9.4,
                    "average_price": 10.0,
                    "unrealized_pnl_pct": -0.06,
                    "ma5": 9.8,
                    "ma20": 9.7,
                }
            ],
        },
        {"positions": {}},
        args,
    )

    with sqlite3.connect(ledger) as conn:
        statuses = dict(conn.execute("select planned_order_id, status from planned_orders").fetchall())
    assert statuses["RISK:2026-07-15:600288:hard_stop_loss"] == "SUPERSEDED_RISK_PLAN"
    assert statuses["RISK:2026-07-15:600288"] == "RISK_CANDIDATE"


def test_risk_scan_uses_intraday_high_for_profit_protection(tmp_path):
    args = SimpleNamespace(
        trade_date="2026-07-16",
        ledger=str(tmp_path / "local_sim.sqlite"),
        account_id="risk-test",
        initial_cash=1_000_000.0,
        hard_stop_pct=0.055,
        profit_protection_min_pct=0.03,
        profit_giveback_pct=0.015,
        profit_retained_ratio=0.65,
        dry_run=True,
    )
    state = {"positions": {}}

    audit = scan_risk(
        {
            "trade_date": "2026-07-16",
            "valuation_complete": True,
            "positions": [
                {
                    "symbol": "600288",
                    "shares": 100,
                    "average_price": 10.0,
                    "market_price": 10.05,
                    "quote_high": 10.4,
                    "unrealized_pnl_pct": 0.005,
                    "ma5": 9.9,
                    "ma20": 9.5,
                }
            ],
        },
        state,
        args,
    )

    candidate = audit["candidates"][0]
    assert candidate["reason_code"] == "profit_giveback"
    assert candidate["peak_unrealized_pnl_pct"] == pytest.approx(0.04)
    assert state["positions"]["600288"]["intraday_high_pnl_pct"] == pytest.approx(0.04)


def test_risk_scan_carries_active_plan_and_confirmation_from_prior_trade_date(tmp_path):
    ledger = tmp_path / "local_sim.sqlite"
    args = SimpleNamespace(
        trade_date="2026-07-16",
        ledger=str(ledger),
        account_id="risk-test",
        initial_cash=1_000_000.0,
        hard_stop_pct=0.055,
        profit_protection_min_pct=0.03,
        profit_giveback_pct=0.015,
        profit_retained_ratio=0.65,
        dry_run=False,
    )
    from chan520_skill.broker_adapter import LocalSimBrokerAdapter, LocalSimBrokerConfig

    adapter = LocalSimBrokerAdapter(LocalSimBrokerConfig(account_id="risk-test", ledger_path=str(ledger)))
    adapter.record_planned_order(
        {
            "planned_order_id": "RISK:2026-07-15:600288",
            "trade_date": "2026-07-15",
            "symbol": "600288",
            "side": "SELL",
            "volume": 100,
            "status": "RISK_CONFIRMED",
            "armed_at": "2026-07-15T14:50:00+08:00",
            "armed_trade_date": "2026-07-15",
            "reason_codes": ["profit_giveback"],
            "peak_unrealized_pnl_pct": 0.04,
            "profit_protection_min_pct": 0.03,
            "profit_giveback_pct": 0.015,
            "profit_retained_ratio": 0.65,
        }
    )
    adapter.mark_planned_order(
        "RISK:2026-07-15:600288",
        "RISK_CONFIRMED",
        "first confirmation",
        {"quote_time": "2026-07-15T14:50:00+08:00", "price": 10.0},
    )

    scan_risk(
        {
            "trade_date": "2026-07-16",
            "valuation_complete": True,
            "positions": [
                {
                    "symbol": "600288",
                    "shares": 100,
                    "average_price": 10.0,
                    "market_price": 9.7,
                    "quote_high": 10.0,
                    "unrealized_pnl_pct": -0.03,
                    "ma5": 9.8,
                    "ma20": 9.9,
                }
            ],
        },
        {"positions": {}},
        args,
    )

    with sqlite3.connect(ledger) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select planned_order_id, trade_date, status, quote_json, payload_json from planned_orders"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["planned_order_id"] == "RISK:2026-07-15:600288"
    assert rows[0]["trade_date"] == "2026-07-16"
    assert rows[0]["status"] == "RISK_CONFIRMED"
    assert "2026-07-15T14:50:00+08:00" in rows[0]["quote_json"]
    assert '"armed_trade_date": "2026-07-15"' in rows[0]["payload_json"]

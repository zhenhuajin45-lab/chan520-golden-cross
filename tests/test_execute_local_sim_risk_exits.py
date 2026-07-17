from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chan520_skill.broker_adapter import BrokerOrderRequest, BrokerSide, LocalSimBrokerAdapter, LocalSimBrokerConfig
from scripts import execute_local_sim_risk_exits as risk_exit


TZ = ZoneInfo("Asia/Shanghai")


def quote_at(timestamp: str):
    return {
        "code": "600288",
        "name": "大恒科技",
        "price": "9.40",
        "open": "9.50",
        "prev_close": "10.00",
        "high": "9.60",
        "low": "9.30",
        "datetime": timestamp,
    }


def make_adapter(tmp_path, *, plan_date="2026-07-16"):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="risk-exit-test", initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            session_date="2026-07-15",
            client_order_id="entry",
        )
    )
    adapter.record_planned_order(
        {
            "planned_order_id": f"RISK:{plan_date}:SHSE.600288",
            "trade_date": plan_date,
            "symbol": "SHSE.600288",
            "side": "SELL",
            "volume": 100,
            "status": "RISK_CANDIDATE",
            "reason_code": "ma20_breakdown",
            "reason_text": "跌破 MA20 且触发硬止损",
            "reason_codes": ["ma20_breakdown", "hard_stop_loss"],
            "reference_ma20": 9.8,
            "average_price": 10.0,
            "hard_stop_pct": 0.055,
        }
    )
    return adapter, ledger


def test_hard_stop_uses_fast_exit_for_t_plus_one_position(tmp_path, monkeypatch):
    adapter, ledger = make_adapter(tmp_path)
    monkeypatch.setattr(risk_exit, "tencent_quote", lambda _code: quote_at("20260716100000"))

    result = risk_exit.run_risk_exit_cycle(
        adapter=adapter,
        ledger=Path(ledger),
        account_id="risk-exit-test",
        trade_date="2026-07-16",
        now=datetime(2026, 7, 16, 10, 0, tzinfo=TZ),
        max_age_minutes=5,
        confirmation_max_minutes=20,
        submit=True,
    )

    assert result["results"][0]["action"] == "SELL"
    assert result["results"][0]["risk_speed"] == "FAST"
    assert set(result["results"][0]["reason_codes"]) == {"ma20_breakdown", "hard_stop_loss"}
    assert result["results"][0]["broker_result"]["accepted"] is True
    assert adapter.account_snapshot()["positions"] == []


def test_ma20_only_exit_still_requires_confirmation(tmp_path, monkeypatch):
    adapter, ledger = make_adapter(tmp_path)
    with sqlite3.connect(ledger) as conn:
        payload = conn.execute(
            "select payload_json from planned_orders where account_id = ?",
            ("risk-exit-test",),
        ).fetchone()[0]
        import json

        data = json.loads(payload)
        data["reason_codes"] = ["ma20_breakdown"]
        conn.execute(
            "update planned_orders set payload_json = ?, reason_code = ?",
            (json.dumps(data), "ma20_breakdown"),
        )
    monkeypatch.setattr(
        risk_exit,
        "tencent_quote",
        lambda _code: {
            **quote_at("20260716100000"),
            "price": "9.70",
        },
    )

    first = risk_exit.run_risk_exit_cycle(
        adapter=adapter,
        ledger=Path(ledger),
        account_id="risk-exit-test",
        trade_date="2026-07-16",
        now=datetime(2026, 7, 16, 10, 0, tzinfo=TZ),
        max_age_minutes=5,
        confirmation_max_minutes=20,
        submit=True,
    )

    assert first["results"][0]["action"] == "CONFIRM"
    assert first["results"][0]["risk_speed"] == "CONFIRMED"
    assert adapter.account_snapshot()["positions"][0]["shares"] == 100


def test_risk_exit_blocks_same_day_position_under_t_plus_one(tmp_path, monkeypatch):
    adapter, ledger = make_adapter(tmp_path, plan_date="2026-07-15")
    monkeypatch.setattr(risk_exit, "tencent_quote", lambda _code: quote_at("20260715100000"))

    result = risk_exit.run_risk_exit_cycle(
        adapter=adapter,
        ledger=Path(ledger),
        account_id="risk-exit-test",
        trade_date="2026-07-15",
        now=datetime(2026, 7, 15, 10, 0, tzinfo=TZ),
        max_age_minutes=5,
        confirmation_max_minutes=20,
        submit=True,
    )

    assert result["results"][0]["action"] == "WAIT"
    assert result["results"][0]["reason"] == "T_PLUS_ONE_WAIT"
    assert adapter.account_snapshot()["positions"][0]["shares"] == 100


def make_zte_replay_adapter(path: Path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="zte-replay", initial_cash=1_000_000.0, ledger_path=str(path))
    )
    adapter.submit_order(
        BrokerOrderRequest(
            symbol="000063",
            side=BrokerSide.BUY,
            volume=2000,
            price=39.91,
            session_date="2026-07-15",
            client_order_id="zte-entry",
        )
    )
    adapter.record_planned_order(
        {
            "planned_order_id": "RISK:2026-07-16:000063",
            "trade_date": "2026-07-16",
            "symbol": "000063",
            "side": "SELL",
            "volume": 2000,
            "status": "RISK_CANDIDATE",
            "reason_code": "profit_giveback",
            "reason_codes": ["profit_giveback", "hard_stop_loss", "ma5_failed_reclaim", "ma20_breakdown"],
            "reference_ma5": 39.54,
            "reference_ma20": 37.63,
            "average_price": 39.91,
            "hard_stop_pct": 0.055,
            "peak_unrealized_pnl_pct": 0.0375845653,
            "profit_protection_min_pct": 0.03,
            "profit_giveback_pct": 0.015,
            "profit_retained_ratio": 0.65,
            "armed_at": "2026-07-16T15:20:00+08:00",
            "armed_trade_date": "2026-07-16",
        }
    )
    return adapter


def zte_quote(price: float, timestamp: str):
    return {
        "code": "000063",
        "name": "中兴通讯",
        "price": str(price),
        "open": "38.97",
        "prev_close": "40.00",
        "high": "39.26",
        "low": str(price),
        "datetime": timestamp,
    }


def test_20260717_opening_replay_executes_profit_protection_before_old_hard_stop(tmp_path, monkeypatch):
    new_adapter = make_zte_replay_adapter(tmp_path / "new.sqlite")
    new_ledger = Path(new_adapter.path)
    quote = zte_quote(38.97, "20260717093000")
    monkeypatch.setattr(risk_exit, "tencent_quote", lambda _code: quote)

    first = risk_exit.run_risk_exit_cycle(
        adapter=new_adapter,
        ledger=new_ledger,
        account_id="zte-replay",
        trade_date="2026-07-17",
        now=datetime(2026, 7, 17, 9, 30, tzinfo=TZ),
        max_age_minutes=5,
        confirmation_max_minutes=20,
        submit=True,
    )
    assert first["results"][0]["action"] == "CONFIRM"

    quote = zte_quote(37.83, "20260717093200")
    second = risk_exit.run_risk_exit_cycle(
        adapter=new_adapter,
        ledger=new_ledger,
        account_id="zte-replay",
        trade_date="2026-07-17",
        now=datetime(2026, 7, 17, 9, 32, tzinfo=TZ),
        max_age_minutes=5,
        confirmation_max_minutes=20,
        submit=True,
    )
    assert second["results"][0]["action"] == "SELL"
    assert second["results"][0]["reason"] == "ma5_failed_reclaim"

    old_adapter = make_zte_replay_adapter(tmp_path / "old.sqlite")
    quote = zte_quote(36.82, "20260717093603")
    old = risk_exit.run_risk_exit_cycle(
        adapter=old_adapter,
        ledger=Path(old_adapter.path),
        account_id="zte-replay",
        trade_date="2026-07-17",
        now=datetime(2026, 7, 17, 9, 36, 7, tzinfo=TZ),
        max_age_minutes=5,
        confirmation_max_minutes=20,
        submit=True,
    )
    assert old["results"][0]["action"] == "SELL"
    assert old["results"][0]["reason"] == "hard_stop_loss"

    new_cash = new_adapter.account_snapshot()["cash"]
    old_cash = old_adapter.account_snapshot()["cash"]
    assert new_cash - old_cash > 2000

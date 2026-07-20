from __future__ import annotations

from datetime import date

from scripts.replay_local_sim_watch_only import INDEX_SYMBOLS, index_data_key, run_replay


def candidate(symbol: str, priority: int, trigger: float) -> dict:
    return {
        "planned_order_id": f"CORE:2026-07-20:{symbol}",
        "order_intent_id": f"CORE-2026-07-20-{priority:02d}",
        "execution_priority": priority,
        "trade_date": "2026-07-20",
        "signal_date": "2026-07-17",
        "symbol": symbol,
        "stock_name": symbol,
        "side": "BUY",
        "volume": 0,
        "status": "WATCH_ONLY",
        "trigger_price": trigger,
        "lower_price": trigger * 0.98,
        "upper_price": trigger * 1.02,
        "invalid_price": trigger * 1.03,
        "stop_price": trigger * 0.94,
        "ma5": trigger * 0.98,
        "ma20": trigger * 0.95,
        "research_only": True,
        "research_cohort": "BEAR_DEFENSIVE_WATCH",
    }


def series(name: str, prev_close: float, first: float, second: float) -> dict:
    return {
        "name": name,
        "prev_close": prev_close,
        "open": first,
        "minutes": {"0930": first, "0932": second},
    }


def test_replay_uses_explicit_risk_priority_and_never_changes_actual_execution_count():
    core = {
        "policy_id": "local_sim_core_plan_v2",
        "market_regime": {"state": "BEAR"},
        "executable_buy_count": 0,
        "account_equity": 500_000.0,
        "plans": [candidate("600001", 2, 10.0), candidate("000001", 1, 20.0)],
    }
    market_data = {
        "600001": series("A", 10.0, 10.0, 10.1),
        "000001": series("B", 20.0, 20.0, 20.2),
    }
    market_data.update({index_data_key(symbol): series(symbol, 100.0, 100.0, 100.0) for symbol in INDEX_SYMBOLS})

    payload = run_replay(
        core,
        date(2026, 7, 20),
        market_data,
        initial_equity=1_000_000.0,
        max_fills=1,
        max_exposure_pct=0.15,
    )

    assert payload["status"] == "PASS"
    assert payload["research_only"] is True
    assert payload["live_execution_enabled"] is False
    assert payload["actual_executable_buy_count"] == 0
    assert payload["replay_equity"] == 500_000.0
    assert payload["filled_count"] == 1
    assert payload["fills"][0]["symbol"] == "000001"
    assert payload["fills"][0]["volume"] == 1200


def test_replay_fails_closed_when_index_minutes_are_missing():
    core = {
        "policy_id": "local_sim_core_plan_v2",
        "market_regime": {"state": "BEAR"},
        "executable_buy_count": 0,
        "plans": [candidate("600001", 1, 10.0)],
    }
    payload = run_replay(
        core,
        date(2026, 7, 20),
        {"600001": series("A", 10.0, 10.0, 10.1)},
        initial_equity=1_000_000.0,
        max_fills=2,
        max_exposure_pct=0.15,
    )

    assert payload["status"] == "FAIL_CLOSED"
    assert payload["filled_count"] == 0
    assert payload["data_complete"] is False

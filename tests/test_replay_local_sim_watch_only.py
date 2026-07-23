from __future__ import annotations

from datetime import date

from scripts.replay_local_sim_watch_only import (
    INDEX_SYMBOLS,
    index_data_key,
    parse_tencent_day_payload,
    research_candidates,
    run_replay,
)


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
    assert payload["fills"][0]["volume"] == 600
    assert payload["position_cap_pct"] == 0.025
    assert payload["sampling_interval_minutes"] == 2
    assert len(payload["individual_candidate_results"]) == 2
    assert len(payload["all_candidate_independent_results"]) == 2
    assert payload["all_candidate_close_summary"]["available_count"] == 2
    assert len(payload["ordering_sensitivity"]["variants"]) == 4
    assert payload["ranked_portfolio"]["ordering"] == "risk_priority"


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


def test_historical_day_uses_matched_day_prec_and_first_minute_not_realtime_quote():
    payload = {
        "data": {
            "sh600001": {
                "data": [
                    {"date": "20260720", "prec": "10.20", "data": ["0930 10.15", "0932 10.30"]}
                ],
                "qt": {"sh600001": ["", "示例股票", "", "", "99.00", "98.00"]},
            }
        }
    }

    result = parse_tencent_day_payload(payload, "sh600001", "600001", date(2026, 7, 20))

    assert result["prev_close"] == 10.20
    assert result["open"] == 10.15
    assert result["historical_price_fields_source"] == "matched_day.prec_and_first_minute"


def test_bear_reconstruction_can_activate_legacy_unknown_watch_sample():
    row = candidate("600001", 1, 10.0)
    row.pop("research_only")
    row.pop("research_cohort")
    row.update({"geometry_valid": True, "score": 20, "rsi14": 60})
    core = {"market_regime": {"state": "UNKNOWN"}, "research_regime": {"state": "BEAR"}, "plans": [row]}

    assert [item["symbol"] for item in research_candidates(core)] == ["600001"]

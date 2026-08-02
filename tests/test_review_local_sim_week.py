from __future__ import annotations

import json
from datetime import date

from scripts import review_local_sim_week as weekly


def test_legacy_full_gate_derivation_only_counts_market_blocked_strict_plan():
    plan = {
        "plans": [
            {"reason_code": "SCAN_WATCH_ONLY", "blocking_reason_codes": ["MARKET_REGIME_BLOCKED"]},
            {"reason_code": "SCAN_WATCH_ONLY", "blocking_reason_codes": ["MARKET_REGIME_BLOCKED", "RR_TOO_LOW"]},
            {"reason_code": "SCAN_WATCH_ONLY", "blocking_reason_codes": ["SCAN_WATCH_ONLY", "STRICT_ENTRY_REQUIRED"]},
        ],
        "execution_funnel": {},
    }

    assert weekly.strict_full_gate_count(plan) == 1


def test_t1_market_day_fetches_and_caches_through_shared_market_loader(monkeypatch):
    monkeypatch.setattr(weekly, "load_minute_day", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        weekly,
        "fetch_market_day",
        lambda symbol, target, **_kwargs: {"symbol": symbol, "trade_date": target.isoformat(), "minutes": {"1500": 10.5}},
    )

    payload, error = weekly.t1_market_day("600001", date(2026, 7, 30))

    assert error == ""
    assert payload["minutes"]["1500"] == 10.5


def test_weekly_review_aggregates_exact_cohort_and_t1_without_touching_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly, "ROOT", tmp_path)
    trade_dates = ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"]
    for index, trade_date in enumerate(trade_dates):
        key = trade_date.replace("-", "")
        plan_dir = tmp_path / "reports" / "local_sim_plan" / key
        replay_dir = tmp_path / "reports" / "local_sim_counterfactual" / key
        daily_dir = tmp_path / "reports" / "local_sim_daily" / key
        plan_dir.mkdir(parents=True)
        replay_dir.mkdir(parents=True)
        daily_dir.mkdir(parents=True)
        (plan_dir / "core_plan.json").write_text(json.dumps({
            "trade_date": trade_date,
            "signal_date": trade_date,
            "market_regime": {"state": "BEAR"},
            "plans": [{"symbol": "600001"}],
            "execution_funnel": {"strict_count": 1, "strict_full_gate_count": 0, "core_executable_count": 0, "bear_defensive_count": 1, "bear_pilot_count": 0},
        }), encoding="utf-8")
        fills = [] if index != 2 else [{"symbol": "600001", "stock_name": "A", "fill_price": 10.0, "volume": 100, "buy_commission": 5.0}]
        (replay_dir / "watch_only_replay.json").write_text(json.dumps({
            "candidate_count": 1,
            "research_candidate_source": "explicit_bear_defensive_cohort",
            "filled_count": len(fills),
            "net_mark_pnl": 10.0 if fills else 0.0,
            "fills": fills,
            "all_candidate_ranked_portfolio": {"filled_count": len(fills), "net_mark_pnl": 10.0 if fills else 0.0},
        }), encoding="utf-8")
        for phase in ("plan", "eod"):
            (daily_dir / f"{phase}_summary.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")

    monkeypatch.setattr(
        weekly,
        "load_minute_day",
        lambda _symbol, session_date, **_kwargs: {"minutes": {"1500": 10.5}} if session_date == date(2026, 7, 30) else None,
    )

    payload = weekly.build_weekly_review(date(2026, 7, 31), sessions=5, ledger=tmp_path / "missing.sqlite")

    assert payload["status"] == "PASS"
    assert payload["trade_dates"] == trade_dates
    assert payload["alerts"] == ["SIGNAL_STARVATION"]
    assert payload["exact_research_same_day_mark"]["net_mark_pnl"] == 10.0
    assert payload["t1_next_close_observation"]["completed_count"] == 1
    assert payload["t1_next_close_observation"]["net_pnl"] > 0
    assert payload["risk_boundary"]["shadow_readiness"] is False

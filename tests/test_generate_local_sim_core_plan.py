from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from chan520_skill.broker_adapter import LocalSimBrokerAdapter, LocalSimBrokerConfig
from scripts import generate_local_sim_core_plan as core_plan


def test_core_plan_only_makes_strict_rows_executable_and_expires_old_plans(tmp_path, monkeypatch):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="plan-test", initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    adapter.initialize_account()
    adapter.record_planned_order(
        {
            "planned_order_id": "CORE:2026-07-15:000001",
            "trade_date": "2026-07-15",
            "symbol": "000001",
            "side": "BUY",
            "volume": 100,
            "status": "WATCH_TRIGGER",
        }
    )
    monkeypatch.setattr(
        core_plan,
        "candidate_levels",
        lambda _row, _signal_date, **_kwargs: {
            "stop": 9.5,
            "target": 12.0,
            "rr": 4.0,
            "t1_loss_buffer_pct": 0.075,
            "reason_codes": [],
        },
    )
    rows = [
        {
            "code": "600288",
            "name": "大恒科技",
            "verdict": "入选",
            "defect_count": "0",
            "score": "25",
            "satisfied_count": "5",
            "close": "10.00",
            "ma5": "9.80",
            "ma10": "9.65",
            "ma20": "9.50",
            "ma60": "9.00",
            "satisfied": "周线趋势向上；日线回踩确认",
        },
        {
            "code": "000001",
            "name": "平安银行",
            "verdict": "观察（量能待确认）",
            "defect_count": "1",
            "score": "18",
            "satisfied_count": "3",
            "close": "10.00",
            "ma5": "9.90",
            "ma20": "9.70",
        },
    ]

    payload = core_plan.generate_plan(
        adapter=adapter,
        ledger=Path(ledger),
        account_id="plan-test",
        trade_date=date(2026, 7, 16),
        signal_date=date(2026, 7, 15),
        scan_path=tmp_path / "scan.csv",
        scan_rows=rows,
        regime={"state": "NORMAL", "regime_ok": True, "detail": "range"},
        max_candidates=20,
        max_buy_plans=2,
        max_new_exposure_pct=0.15,
        risk_per_plan_pct=0.005,
    )

    assert payload["status"] == "PASS"
    assert payload["expired_plan_count"] == 1
    assert payload["executable_buy_count"] == 1
    assert payload["buy_entry_ready"] is True
    assert payload["execution_readiness"] == "TRADE_READY"
    strict = next(row for row in payload["plans"] if row["symbol"] == "600288")
    watch = next(row for row in payload["plans"] if row["symbol"] == "000001")
    assert strict["status"] == "WATCH_TRIGGER"
    assert strict["volume"] > 0
    assert strict["local_sim_execution_policy_id"] == core_plan.POLICY_ID
    assert watch["status"] == "WATCH_ONLY"
    assert watch["volume"] == 0
    assert watch["level_evidence_status"] == "NOT_COMPUTED_WATCH_ONLY"
    assert watch["target_price_available"] is False


def test_unknown_market_regime_generates_no_executable_buys(tmp_path, monkeypatch):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="plan-test", initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    monkeypatch.setattr(
        core_plan,
        "candidate_levels",
        lambda _row, _signal_date, **_kwargs: {
            "stop": 9.5,
            "target": 12.0,
            "rr": 4.0,
            "t1_loss_buffer_pct": 0.075,
            "reason_codes": [],
        },
    )

    payload = core_plan.generate_plan(
        adapter=adapter,
        ledger=Path(ledger),
        account_id="plan-test",
        trade_date=date(2026, 7, 16),
        signal_date=date(2026, 7, 15),
        scan_path=tmp_path / "scan.csv",
        scan_rows=[
            {
                "code": "600288",
                "name": "大恒科技",
                "verdict": "入选",
                "defect_count": "0",
                "score": "25",
                "close": "10.00",
                "ma5": "9.80",
                "ma20": "9.50",
            }
        ],
        regime={"state": "UNKNOWN", "regime_ok": False, "detail": "unavailable"},
        max_candidates=20,
        max_buy_plans=2,
        max_new_exposure_pct=0.15,
        risk_per_plan_pct=0.005,
    )

    assert payload["status"] == "FAIL_CLOSED"
    assert payload["executable_buy_count"] == 0
    assert payload["buy_entry_ready"] is False
    assert payload["execution_readiness"] == "RISK_ONLY"
    assert payload["plans"][0]["status"] == "WATCH_ONLY"
    assert "MARKET_REGIME_BLOCKED" in payload["plans"][0]["reason_codes"]


def test_low_scan_coverage_fails_closed_even_for_strict_candidate(tmp_path, monkeypatch):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="plan-test", initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    monkeypatch.setattr(
        core_plan,
        "candidate_levels",
        lambda _row, _signal_date, **_kwargs: {
            "stop": 9.5,
            "target": 12.0,
            "rr": 4.0,
            "t1_loss_buffer_pct": 0.075,
            "reason_codes": [],
        },
    )

    payload = core_plan.generate_plan(
        adapter=adapter,
        ledger=Path(ledger),
        account_id="plan-test",
        trade_date=date(2026, 7, 16),
        signal_date=date(2026, 7, 15),
        scan_path=tmp_path / "scan.csv",
        scan_rows=[
            {
                "code": "600288",
                "name": "大恒科技",
                "verdict": "入选",
                "defect_count": "0",
                "score": "25",
                "close": "10.00",
                "ma5": "9.80",
                "ma20": "9.50",
            }
        ],
        regime={"state": "NORMAL", "regime_ok": True, "detail": "range"},
        scan_quality={"coverage": 0.55, "minimum_coverage": 0.85, "coverage_pass": False},
        max_candidates=20,
        max_buy_plans=2,
        max_new_exposure_pct=0.15,
        risk_per_plan_pct=0.005,
    )

    assert payload["status"] == "FAIL_CLOSED"
    assert payload["executable_buy_count"] == 0
    assert payload["plans"][0]["status"] == "WATCH_ONLY"
    assert "SCAN_COVERAGE_BLOCKED" in payload["plans"][0]["blocking_reason_codes"]


def test_invalid_entry_geometry_is_never_executable(tmp_path, monkeypatch):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="plan-test", initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    monkeypatch.setattr(
        core_plan,
        "candidate_levels",
        lambda _row, _signal_date, **_kwargs: {
            "stop": 10.2,
            "target": 13.0,
            "rr": 4.0,
            "t1_loss_buffer_pct": 0.075,
            "reason_codes": [],
        },
    )

    payload = core_plan.generate_plan(
        adapter=adapter,
        ledger=Path(ledger),
        account_id="plan-test",
        trade_date=date(2026, 7, 16),
        signal_date=date(2026, 7, 15),
        scan_path=tmp_path / "scan.csv",
        scan_rows=[
            {
                "code": "600288",
                "name": "大恒科技",
                "verdict": "入选",
                "defect_count": "0",
                "score": "25",
                "close": "10.50",
                "ma5": "10.00",
                "ma20": "9.50",
            }
        ],
        regime={"state": "NORMAL", "regime_ok": True, "detail": "range"},
        max_candidates=20,
        max_buy_plans=2,
        max_new_exposure_pct=0.15,
        risk_per_plan_pct=0.005,
    )

    plan = payload["plans"][0]
    assert plan["status"] == "WATCH_ONLY"
    assert plan["volume"] == 0
    assert plan["geometry_valid"] is False
    assert "INVALID_PLAN_GEOMETRY" in plan["blocking_reason_codes"]
    assert "TRIGGER_BELOW_LOWER" in plan["blocking_reason_codes"]


def test_t1_loss_buffer_reduces_high_volatility_position_size():
    shares = core_plan.planned_shares(
        equity=1_000_000,
        entry=40.0,
        stop=37.8,
        score=25,
        risk_per_plan_pct=0.005,
        t1_loss_buffer_pct=0.12,
    )

    assert shares == 1000


def test_bear_market_creates_research_only_defensive_cohort_without_live_buys(tmp_path):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id="plan-test", initial_cash=1_000_000.0, ledger_path=str(ledger))
    )
    rows = [
        {
            "code": "600177",
            "name": "雅戈尔",
            "verdict": "观察（等待确认）",
            "defect_count": "1",
            "score": "20",
            "close": "10.00",
            "ma5": "9.90",
            "ma20": "9.50",
            "rsi14": "62",
        },
        {
            "code": "001314",
            "name": "亿道信息",
            "verdict": "观察（等待确认）",
            "defect_count": "1",
            "score": "17",
            "close": "10.00",
            "ma5": "9.90",
            "ma20": "9.50",
            "rsi14": "56",
        },
    ]

    payload = core_plan.generate_plan(
        adapter=adapter,
        ledger=Path(ledger),
        account_id="plan-test",
        trade_date=date(2026, 7, 20),
        signal_date=date(2026, 7, 17),
        scan_path=tmp_path / "scan.csv",
        scan_rows=rows,
        regime={"state": "BEAR", "regime_ok": False, "detail": "down"},
        max_candidates=20,
        max_buy_plans=2,
        max_new_exposure_pct=0.15,
        risk_per_plan_pct=0.005,
    )

    cohort = payload["research_cohorts"]["bear_defensive"]
    assert payload["executable_buy_count"] == 0
    assert cohort["symbols"] == ["600177"]
    assert cohort["live_execution_enabled"] is False
    defensive = next(row for row in payload["plans"] if row["symbol"] == "600177")
    assert defensive["status"] == "WATCH_ONLY"
    assert defensive["volume"] == 0
    assert defensive["research_only"] is True
    assert defensive["research_cohort"] == "BEAR_DEFENSIVE_WATCH"


def test_execution_priority_prefers_lower_t1_and_volatility_risk():
    low_risk = {"t1_loss_buffer_pct": 0.075, "average_amplitude_pct": 0.02, "rr": 2.1}
    high_risk = {"t1_loss_buffer_pct": 0.12, "average_amplitude_pct": 0.01, "rr": 4.0}

    assert core_plan.execution_risk_priority_key({"code": "600001"}, low_risk) < core_plan.execution_risk_priority_key(
        {"code": "300001"}, high_risk
    )


def test_supplemental_ths_context_is_audited_but_not_an_execution_gate(tmp_path, monkeypatch):
    context_dir = tmp_path / "reports" / "market_context" / "20260721"
    context_dir.mkdir(parents=True)
    (context_dir / "ths_data_center.json").write_text(
        json.dumps(
            {
                "trade_date": "2026-07-21",
                "generated_at": "2026-07-22T08:00:00+08:00",
                "audit": {"passed": True, "endpoint_count": 17, "failed_count": 0},
                "limit_up": {"emotion_status": "PASS", "emotion_label": "涨停情绪偏强"},
                "display": {"summary_line": "涨停 120", "hot_topics": ["芯片"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(core_plan, "ROOT", tmp_path)

    context = core_plan.load_supplemental_market_context(date(2026, 7, 21))

    assert context["status"] == "PASS"
    assert context["emotion_label"] == "涨停情绪偏强"
    assert context["used_for_execution_gate"] is False
    assert context["role"] == "supplemental_validation_only"

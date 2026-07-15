from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from chan520_skill.live_execution import (
    LiveExecutionBlocked,
    build_pending_execution_plan,
    pending_row_to_broker_request,
    to_gm_symbol,
)


def make_alpha_store(path: Path, max_date: str = "2026-07-14") -> None:
    conn = sqlite3.connect(path)
    try:
        for table in ("daily_bars", "dynamic_universe", "index_bars", "instrument_status"):
            conn.execute(f"create table {table}(trade_date text)")
            conn.execute(f"insert into {table}(trade_date) values (?)", (max_date,))
        conn.commit()
    finally:
        conn.close()


def make_paper_store(path: Path, decision_date: str = "2026-07-14") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            create table pending_orders(
                run_id text,
                pending_order_id text,
                session_date text,
                order_intent_id text,
                payload_hash text,
                payload_json text
            )
            """
        )
        payload = {
            "decision_date": decision_date,
            "code": "600288",
            "side": "buy",
            "shares": 100,
            "signal_close": "15.55",
            "pending_order_id": "pend1",
            "order_intent_id": "intent1",
            "candidate_id": "cand1",
            "planned_stop": "14.80",
            "planned_target": "17.00",
            "rr": "1.5",
            "reason": "strategy_v5_alpha_ranked_entry",
        }
        conn.execute(
            "insert into pending_orders values (?, ?, ?, ?, ?, ?)",
            ("run1", "pend1", decision_date, "intent1", "hash", json.dumps(payload, sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()


def test_build_pending_execution_plan_rejects_stale_alpha_store(tmp_path):
    alpha = tmp_path / "alpha.sqlite"
    paper = tmp_path / "paper.sqlite"
    make_alpha_store(alpha, "2026-07-09")
    make_paper_store(paper)

    with pytest.raises(LiveExecutionBlocked, match="stale"):
        build_pending_execution_plan(
            alpha_store=alpha,
            paper_store=paper,
            run_id="run1",
            execution_date=date(2026, 7, 15),
            decision_date=date(2026, 7, 14),
        )


def test_build_pending_execution_plan_requires_decision_date_pending(tmp_path):
    alpha = tmp_path / "alpha.sqlite"
    paper = tmp_path / "paper.sqlite"
    make_alpha_store(alpha)
    make_paper_store(paper, "2026-07-09")

    with pytest.raises(LiveExecutionBlocked, match="no pending orders"):
        build_pending_execution_plan(
            alpha_store=alpha,
            paper_store=paper,
            run_id="run1",
            execution_date=date(2026, 7, 15),
            decision_date=date(2026, 7, 14),
        )


def test_pending_row_to_broker_request_maps_buy_to_open():
    row = {
        "decision_date": "2026-07-14",
        "code": "600288",
        "side": "buy",
        "shares": 100,
        "signal_close": "15.55",
        "pending_order_id": "pend1",
        "order_intent_id": "intent1",
    }

    request = pending_row_to_broker_request(row, execution_date=date(2026, 7, 15), run_id="run1")

    assert request.symbol == "SHSE.600288"
    assert request.side.value == "BUY"
    assert request.position_effect == "OPEN"
    assert request.volume == 100
    assert request.price == 15.55


def test_to_gm_symbol_exchange_prefixes():
    assert to_gm_symbol("600288") == "SHSE.600288"
    assert to_gm_symbol("000001") == "SZSE.000001"
    assert to_gm_symbol("300001") == "SZSE.300001"
    assert to_gm_symbol("SHSE.600288") == "SHSE.600288"

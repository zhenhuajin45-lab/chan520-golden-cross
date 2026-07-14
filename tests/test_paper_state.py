from __future__ import annotations

from datetime import date

import pytest

from chan520_skill.paper_state import (
    IdempotencyConflict,
    PaperStateStore,
    PortfolioState,
    SessionInput,
    process_session_close,
    process_session_open,
)


def test_paper_state_store_phase_and_lifecycle_idempotency(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(
            run_id="run1",
            cohort_id="cohort1",
            strategy_commit="abc123",
            full_config_hash="cfg",
            audit_schema_version="2",
            initial_cash=100000.0,
        )
        day = date(2026, 1, 5)
        store.record_session("run1", day, "open", "DONE", {"day": day.isoformat()})
        store.record_session("run1", day, "open", "DONE", {"day": day.isoformat()})
        payload = {
            "candidate_rows": [{"candidate_id": "cand1", "date": day.isoformat(), "selected": 1, "rank": 1}],
            "pending_rows": [
                {
                    "pending_order_id": "pend1",
                    "order_intent_id": "intent1",
                    "candidate_id": "cand1",
                    "decision_date": day.isoformat(),
                }
            ],
            "fill_rows": [
                {
                    "fill_id": "fill1",
                    "date": day.isoformat(),
                    "code": "600288",
                    "side": "buy",
                    "price": 10.0,
                    "shares": 100,
                    "fee": 5.0,
                    "pending_order_id": "pend1",
                    "position_id": "pos1",
                    "trade_id": "",
                }
            ],
            "trade_rows": [],
            "position_link_rows": [
                {"position_id": "pos1", "fill_id": "fill1", "trade_id": "", "fill_role": "initial_entry"}
            ],
            "equity_payload": {"cash": 98995.0, "equity": 100000.0, "exposure": 0.01},
        }
        store.ingest_kernel_day(run_id="run1", session_date=day, **payload)
        store.ingest_kernel_day(run_id="run1", session_date=day, **payload)

        assert store.count("paper_sessions") == 1
        assert store.count("candidate_snapshots") == 1
        assert store.count("pending_orders") == 1
        assert store.count("fills") == 1
        assert store.count("positions") == 1
        assert store.count("position_fill_links") == 1
        assert store.duplicate_count("fills", "fill_id", "run1") == 0
        assert store.orphan_count("run1") == 0
    finally:
        store.close()


def test_paper_state_store_transaction_rolls_back(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(
            run_id="run1",
            cohort_id="cohort1",
            strategy_commit="abc123",
            full_config_hash="cfg",
            audit_schema_version="2",
            initial_cash=100000.0,
        )
        before = store.count("reconciliation_results")
        try:
            with store.conn:
                store.conn.execute(
                    "insert into reconciliation_results values (?, ?, ?, ?, ?, ?)",
                    ("run1", "2026-01-05", "probe", "STARTED", "hash", "{}"),
                )
                raise RuntimeError("probe")
        except RuntimeError:
            pass
        assert store.count("reconciliation_results") == before
    finally:
        store.close()


def test_paper_state_store_rejects_idempotency_conflict(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(
            run_id="run1",
            cohort_id="cohort1",
            strategy_commit="abc123",
            full_config_hash="cfg",
            audit_schema_version="2",
            initial_cash=100000.0,
        )
        with pytest.raises(IdempotencyConflict):
            store.init_run(
                run_id="run1",
                cohort_id="cohort1",
                strategy_commit="different",
                full_config_hash="cfg",
                audit_schema_version="2",
                initial_cash=100000.0,
            )
    finally:
        store.close()


def test_session_open_close_mutate_state_and_persist_snapshots(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(
            run_id="run1",
            cohort_id="cohort1",
            strategy_commit="abc123",
            full_config_hash="cfg",
            audit_schema_version="2",
            initial_cash=100000.0,
        )
        state = PortfolioState(
            run_id="run1",
            state_version="2",
            cash=100000.0,
            previous_close_equity=100000.0,
            strategy_commit="abc123",
            full_config_hash="cfg",
            audit_schema_version="2",
        )
        day = date(2026, 1, 6)
        close_input = SessionInput(
            session_date=day,
            candidates=[{"candidate_id": "cand1", "date": day.isoformat(), "selected": 1, "rank": 1}],
            execution_rows={
                "pending_rows": [
                    {
                        "pending_order_id": "pend1",
                        "order_intent_id": "intent1",
                        "candidate_id": "cand1",
                        "decision_date": day.isoformat(),
                        "code": "600288",
                        "side": "buy",
                    }
                ]
            },
            equity_payload={"cash": 100000.0, "equity": 100000.0, "exposure": 0.0},
        )
        before_close = PortfolioState(**state.__dict__)
        close_result = process_session_close(state, close_input)
        store.persist_session_result("run1", day, "close", before_close, close_result)

        next_day = date(2026, 1, 7)
        open_input = SessionInput(
            session_date=next_day,
            execution_rows={
                "fill_rows": [
                    {
                        "fill_id": "fill1",
                        "date": next_day.isoformat(),
                        "code": "600288",
                        "side": "buy",
                        "price": 10.0,
                        "shares": 100,
                        "fee": 5.0,
                        "pending_order_id": "pend1",
                        "position_id": "pos1",
                        "trade_id": "",
                    }
                ],
                "position_link_rows": [
                    {"position_id": "pos1", "fill_id": "fill1", "trade_id": "", "fill_role": "initial_entry"}
                ],
            },
            equity_payload={"cash": 98995.0, "equity": 100000.0, "exposure": 0.01},
        )
        before_open = PortfolioState(**state.__dict__)
        open_result = process_session_open(state, open_input)
        store.persist_session_result("run1", next_day, "open", before_open, open_result)

        assert store.count("portfolio_state_snapshots") == 2
        assert store.count("ledger_events") == 2
        assert store.count("pending_orders") == 1
        assert store.count("fills") == 1
        assert store.load_latest_state("run1").cash == 98995.0
    finally:
        store.close()

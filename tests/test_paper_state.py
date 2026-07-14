from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest

from chan520_skill.paper_state import (
    ContextIdentityMismatch,
    HistoryRewriteDetected,
    IdempotencyConflict,
    MissingPositionMark,
    PaperRunIdentity,
    PaperStateStore,
    PhaseOrderViolation,
    PortfolioState,
    SessionInput,
    build_paper_session_identity,
    process_session_close,
    process_session_open,
    state_payload,
)


def identity(
    *,
    strategy_commit: str = "abc123",
    source_tree_hash: str = "source-hash",
    full_config_hash: str = "cfg",
    data_policy_version: str = "policy",
    audit_schema_version: str = "2",
    cohort_start_date: str = "2026-01-05",
    universe_policy_hash: str = "universe-policy",
    calendar_provider_version: str = "calendar-provider",
) -> PaperRunIdentity:
    return PaperRunIdentity(
        strategy_commit=strategy_commit,
        source_tree_hash=source_tree_hash,
        full_config_hash=full_config_hash,
        data_policy_version=data_policy_version,
        audit_schema_version=audit_schema_version,
        cohort_start_date=cohort_start_date,
        universe_policy_hash=universe_policy_hash,
        calendar_provider_version=calendar_provider_version,
    )


def portfolio_state(run_id: str = "run1", run_identity: PaperRunIdentity | None = None) -> PortfolioState:
    run_identity = run_identity or identity()
    return PortfolioState(
        run_id=run_id,
        state_version="2",
        cash=100000.0,
        previous_close_equity=100000.0,
        **run_identity.as_payload(),
    )


def session_identity(day: date, prior: str = "", prefix: str = "prefix", snapshot: str = "daily"):
    return build_paper_session_identity(
        session_date=day,
        prefix_context_hash=f"{prefix}-{day.isoformat()}",
        calendar_prefix_hash=f"calendar-{day.isoformat()}",
        daily_data_snapshot_hash=f"{snapshot}-{day.isoformat()}",
        prior_history_chain_hash=prior,
    )


def test_paper_state_store_phase_and_lifecycle_idempotency(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(
            run_id="run1",
            cohort_id="cohort1",
            identity=identity(),
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
            identity=identity(),
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
            identity=identity(),
            initial_cash=100000.0,
        )
        with pytest.raises(IdempotencyConflict):
            store.init_run(
                run_id="run1",
                cohort_id="cohort1",
                identity=identity(strategy_commit="different"),
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
            identity=identity(),
            initial_cash=100000.0,
        )
        state = portfolio_state()
        day = date(2026, 1, 6)
        initial_open = SessionInput(
            session_date=day,
            equity_payload={"cash": 100000.0, "equity": 100000.0, "exposure": 0.0},
        )
        before_initial_open = deepcopy(state)
        initial_open_result = process_session_open(state, initial_open)
        store.persist_session_result("run1", day, "open", before_initial_open, initial_open_result)

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
        before_close = deepcopy(state)
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
        before_open = deepcopy(state)
        open_result = process_session_open(state, open_input)
        store.persist_session_result("run1", next_day, "open", before_open, open_result)

        assert store.count("portfolio_state_snapshots") == 3
        assert store.count("ledger_events") == 2
        assert store.count("pending_orders") == 1
        assert store.count("fills") == 1
        assert store.load_latest_state("run1").cash == 98995.0
    finally:
        store.close()


def test_process_session_rejects_legacy_date_overload():
    state = PortfolioState(run_id="run1", state_version="2", cash=100000.0)
    with pytest.raises(TypeError):
        process_session_open(state, date(2026, 1, 5))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        process_session_close(state, date(2026, 1, 5))  # type: ignore[arg-type]


def test_phase_order_rejects_close_before_open(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(
            run_id="run1",
            cohort_id="cohort1",
            identity=identity(),
            initial_cash=100000.0,
        )
        state = portfolio_state()
        day = date(2026, 1, 5)
        result = process_session_close(state, SessionInput(session_date=day))
        with pytest.raises(PhaseOrderViolation):
            store.persist_session_result("run1", day, "close", deepcopy(state), result)
    finally:
        store.close()


def test_context_identity_mismatch_rejected(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(
            run_id="run1",
            cohort_id="cohort1",
            identity=identity(full_config_hash="cfg-a"),
            initial_cash=100000.0,
        )
        state = portfolio_state(run_identity=identity(full_config_hash="cfg-b"))
        day = date(2026, 1, 5)
        result = process_session_open(state, SessionInput(session_date=day))
        with pytest.raises(ContextIdentityMismatch):
            store.persist_session_result("run1", day, "open", deepcopy(state), result)
    finally:
        store.close()


def test_create_run_then_open_identity_matches(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    run_identity = identity()
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=run_identity, initial_cash=100000.0)
        state = store.load_latest_state("run1")
        assert state is not None
        day = date(2026, 1, 5)
        before_open = deepcopy(state)
        result = process_session_open(state, SessionInput(session_date=day, prior_close_equity=100000.0))
        store.persist_session_result("run1", day, "open", before_open, result)
        assert store.count("portfolio_state_snapshots") == 1
    finally:
        store.close()


def test_create_run_then_close_identity_matches(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    run_identity = identity()
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=run_identity, initial_cash=100000.0)
        state = store.load_latest_state("run1")
        assert state is not None
        day = date(2026, 1, 5)
        before_open = deepcopy(state)
        open_result = process_session_open(state, SessionInput(session_date=day, prior_close_equity=100000.0))
        store.persist_session_result("run1", day, "open", before_open, open_result)
        before_close = deepcopy(state)
        close_result = process_session_close(state, SessionInput(session_date=day, reported_equity=100000.0))
        store.persist_session_result("run1", day, "close", before_close, close_result)
        assert store.count("portfolio_state_snapshots") == 2
    finally:
        store.close()


def test_blank_run_identity_rejected(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        with pytest.raises(ContextIdentityMismatch):
            store.init_run(
                run_id="run1",
                cohort_id="cohort1",
                identity=identity(universe_policy_hash=""),
                initial_cash=100000.0,
            )
    finally:
        store.close()


def test_open_preserves_prior_close_equity():
    state = portfolio_state()
    state.previous_close_equity = 123456.0
    result = process_session_open(
        state,
        SessionInput(
            session_date=date(2026, 1, 5),
            prior_close_equity=111111.0,
            equity_payload={"cash": 100000.0, "equity": 999999.0},
        ),
    )
    assert result.state.previous_close_equity == 111111.0


def test_close_uses_market_marks_not_cost():
    state = portfolio_state()
    state.cash = 90000.0
    state.positions = {
        "pos1": {
            "position_id": "pos1",
            "code": "600288",
            "shares": 100,
            "average_price": 10.0,
        }
    }
    result = process_session_close(
        state,
        SessionInput(
            session_date=date(2026, 1, 5),
            marks_by_code={"600288": 12.0},
            reported_equity=91200.0,
        ),
    )
    assert result.equity_snapshot["derived_positions_value"] == 1200.0
    assert result.state.previous_close_equity == 91200.0


def test_missing_active_position_mark_fails_closed():
    state = portfolio_state()
    state.cash = 90000.0
    state.positions = {
        "pos1": {
            "position_id": "pos1",
            "code": "600288",
            "shares": 100,
            "average_price": 10.0,
        }
    }
    with pytest.raises(MissingPositionMark):
        process_session_close(
            state,
            SessionInput(session_date=date(2026, 1, 5), reported_equity=91000.0),
        )


def test_failed_gate_can_retry_same_day(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=identity(), initial_cash=100000.0)
        day = date(2026, 1, 5)
        store.record_failed_session_attempt(
            "run1",
            day,
            "open",
            error_code="FAIL_CLOSED",
            details={"snapshot_hash": "bad"},
        )
        state = store.load_latest_state("run1")
        assert state is not None
        before_open = deepcopy(state)
        result = process_session_open(state, SessionInput(session_date=day, prior_close_equity=100000.0))
        store.persist_session_result("run1", day, "open", before_open, result)
        unresolved = store.conn.execute(
            "select count(*) from paper_session_attempts where resolved_at is null"
        ).fetchone()[0]
        assert unresolved == 0
    finally:
        store.close()


def test_failed_gate_blocks_next_session(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=identity(), initial_cash=100000.0)
        failed_day = date(2026, 1, 5)
        next_day = date(2026, 1, 6)
        store.record_failed_session_attempt(
            "run1",
            failed_day,
            "open",
            error_code="FAIL_CLOSED",
            details={"snapshot_hash": "bad"},
        )
        state = store.load_latest_state("run1")
        assert state is not None
        before_open = deepcopy(state)
        result = process_session_open(state, SessionInput(session_date=next_day, prior_close_equity=100000.0))
        with pytest.raises(PhaseOrderViolation):
            store.persist_session_result("run1", next_day, "open", before_open, result)
    finally:
        store.close()


def test_failed_gate_second_attempt_commits(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=identity(), initial_cash=100000.0)
        day = date(2026, 1, 5)
        store.record_failed_session_attempt(
            "run1",
            day,
            "open",
            error_code="FAIL_CLOSED",
            details={"snapshot_hash": "bad-1"},
        )
        store.record_failed_session_attempt(
            "run1",
            day,
            "open",
            error_code="FAIL_CLOSED",
            details={"snapshot_hash": "bad-2"},
        )
        state = store.load_latest_state("run1")
        assert state is not None
        before_open = deepcopy(state)
        result = process_session_open(state, SessionInput(session_date=day, prior_close_equity=100000.0))
        store.persist_session_result("run1", day, "open", before_open, result)
        assert store.count("paper_sessions") == 1
        unresolved = store.conn.execute(
            "select count(*) from paper_session_attempts where resolved_at is null"
        ).fetchone()[0]
        assert unresolved == 0
    finally:
        store.close()


def test_incremental_prefix_run_identity_stable_across_days(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    run_identity = identity()
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=run_identity, initial_cash=100000.0)
        store.init_run(run_id="run1", cohort_id="cohort1", identity=run_identity, initial_cash=100000.0)
        assert store.count("paper_runs") == 1
    finally:
        store.close()


def test_incremental_prefix_session_identity_changes():
    day1 = date(2026, 1, 5)
    day2 = date(2026, 1, 6)
    sid1 = session_identity(day1)
    sid2 = session_identity(day2, prior=sid1.history_chain_hash)

    assert sid1.prefix_context_hash != sid2.prefix_context_hash
    assert sid1.calendar_prefix_hash != sid2.calendar_prefix_hash
    assert sid1.daily_data_snapshot_hash != sid2.daily_data_snapshot_hash
    assert sid1.history_chain_hash != sid2.history_chain_hash


def test_day2_open_does_not_conflict_with_day1_run(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    run_identity = identity()
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=run_identity, initial_cash=100000.0)
        state = store.load_latest_state("run1")
        assert state is not None

        day1 = date(2026, 1, 5)
        sid1 = session_identity(day1)
        before_open1 = deepcopy(state)
        open1 = process_session_open(
            state,
            SessionInput(session_date=day1, prior_close_equity=100000.0, session_identity=sid1),
        )
        store.persist_session_result("run1", day1, "open", before_open1, open1)
        before_close1 = deepcopy(state)
        close1 = process_session_close(state, SessionInput(session_date=day1, reported_equity=100000.0))
        store.persist_session_result("run1", day1, "close", before_close1, close1)

        day2 = date(2026, 1, 6)
        sid2 = session_identity(day2, prior=state.history_chain_hash)
        before_open2 = deepcopy(state)
        open2 = process_session_open(
            state,
            SessionInput(session_date=day2, prior_close_equity=100000.0, session_identity=sid2),
        )
        store.persist_session_result("run1", day2, "open", before_open2, open2)

        assert store.count("paper_runs") == 1
        assert store.count("portfolio_state_snapshots") == 3
        assert store.load_latest_state("run1").history_chain_hash == sid2.history_chain_hash
    finally:
        store.close()


def test_config_change_rejected(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=identity(full_config_hash="cfg-a"), initial_cash=100000.0)
        with pytest.raises(IdempotencyConflict):
            store.init_run(
                run_id="run1",
                cohort_id="cohort1",
                identity=identity(full_config_hash="cfg-b"),
                initial_cash=100000.0,
            )
    finally:
        store.close()


def test_history_chain_extension_passes(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=identity(), initial_cash=100000.0)
        state = store.load_latest_state("run1")
        assert state is not None
        day = date(2026, 1, 5)
        sid = session_identity(day, prior=state.history_chain_hash)
        before = deepcopy(state)
        result = process_session_open(state, SessionInput(session_date=day, session_identity=sid))
        store.persist_session_result("run1", day, "open", before, result)
        assert store.load_latest_state("run1").history_chain_hash == sid.history_chain_hash
    finally:
        store.close()


def test_history_rewrite_rejected(tmp_path):
    store = PaperStateStore(tmp_path / "paper.sqlite")
    try:
        store.init_run(run_id="run1", cohort_id="cohort1", identity=identity(), initial_cash=100000.0)
        state = store.load_latest_state("run1")
        assert state is not None
        day = date(2026, 1, 5)
        rewritten = session_identity(day, prior="not-the-persisted-prior")
        before = deepcopy(state)
        result = process_session_open(state, SessionInput(session_date=day, session_identity=rewritten))
        with pytest.raises(HistoryRewriteDetected):
            store.persist_session_result("run1", day, "open", before, result)
    finally:
        store.close()


def test_canonical_state_payload_normalizes_positions_pending_and_risk():
    state = portfolio_state()
    state.positions = {
        "600288": 100,
        "pos2": {
            "position_id": "pos2",
            "code": "000001",
            "shares": "200",
            "average_price": "10.5",
            "entry_date": "2026-01-05",
        },
    }
    state.pending_orders = {
        "pend1": "buy",
        "pend2": {
            "pending_order_id": "pend2",
            "order_intent_id": "intent2",
            "code": "000001",
            "side": "sell",
            "shares": "200",
        },
    }
    state.risk_state = {
        "peak_equity": "100000",
        "halted_next_session": False,
        "history_chain_hash": "kept-as-extra",
    }

    payload = state_payload(state)

    assert payload["positions"]["600288"]["position_id"] == "600288"
    assert payload["positions"]["600288"]["shares"] == 100
    assert payload["positions"]["pos2"]["average_price"] == 10.5
    assert payload["pending_orders"]["pend1"]["pending_order_id"] == "pend1"
    assert payload["pending_orders"]["pend1"]["side"] == "buy"
    assert payload["pending_orders"]["pend2"]["shares"] == 200
    assert payload["risk_state"]["peak_equity"] == 100000.0
    assert payload["risk_state"]["history_chain_hash"] == "kept-as-extra"

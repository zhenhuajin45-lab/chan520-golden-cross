from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .evidence_manifest import stable_hash_json


PAPER_STATE_VERSION = "2"
TERMINAL_PHASE_STATUSES = {"COMMITTED", "FAILED"}


class IdempotencyConflict(RuntimeError):
    """Raised when a stable ledger id is replayed with different economic data."""


@dataclass(frozen=True)
class SessionInput:
    session_date: date
    execution_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    signal_rows: list[dict[str, Any]] = field(default_factory=list)
    points: dict[str, Any] = field(default_factory=dict)
    regime: Any | None = None
    sector_states: dict[str, Any] = field(default_factory=dict)
    eligible_symbols: set[str] = field(default_factory=set)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    data_snapshot_hash: str = ""
    equity_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PortfolioState:
    run_id: str
    state_version: str
    last_session_date: date | None = None
    last_completed_phase: str = ""
    cash: float = 0.0
    positions: dict[str, Any] = field(default_factory=dict)
    pending_orders: dict[str, Any] = field(default_factory=dict)
    risk_state: dict[str, Any] = field(default_factory=dict)
    previous_close_equity: float = 0.0
    strategy_commit: str = ""
    full_config_hash: str = ""
    prepared_context_hash: str = ""
    audit_schema_version: str = ""

    @property
    def last_processed_date(self) -> date | None:
        return self.last_session_date

    @last_processed_date.setter
    def last_processed_date(self, value: date | None) -> None:
        self.last_session_date = value


@dataclass
class SessionResult:
    state: PortfolioState
    candidates: list[dict[str, Any]] = field(default_factory=list)
    order_intents: list[dict[str, Any]] = field(default_factory=list)
    pending_orders: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    position_fill_links: list[dict[str, Any]] = field(default_factory=list)
    equity_snapshot: dict[str, Any] = field(default_factory=dict)
    reconciliation: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)


def process_session_open(
    state: PortfolioState,
    session_input: SessionInput | date,
    market_data: dict[str, Any] | None = None,
    config: Any | None = None,
) -> SessionResult | PortfolioState:
    if isinstance(session_input, date):
        state.last_session_date = session_input
        state.last_completed_phase = "open"
        return state
    _ = config
    fills = _rows(session_input.execution_rows, "fill_rows")
    trades = _rows(session_input.execution_rows, "trade_rows")
    links = _rows(session_input.execution_rows, "position_link_rows")
    pending_before = dict(state.pending_orders)
    events: list[dict[str, Any]] = []
    for fill in fills:
        _apply_fill_to_state(state, fill)
        pending_id = _fill_pending_order_id(fill)
        if pending_id:
            state.pending_orders.pop(pending_id, None)
        events.append(
            {
                "event": "fill_applied",
                "fill_id": fill.get("fill_id", ""),
                "pending_order_id": pending_id,
                "position_id": fill.get("position_id", ""),
                "side": fill.get("side", ""),
            }
        )
    equity = _equity_payload(state, session_input, market_data)
    state.cash = float(equity.get("cash", state.cash))
    state.previous_close_equity = float(equity.get("equity", state.previous_close_equity or state.cash))
    state.last_session_date = session_input.session_date
    state.last_completed_phase = "open"
    state.risk_state["last_open_data_snapshot_hash"] = session_input.data_snapshot_hash
    reconciliation = _reconcile_state(state, equity)
    reconciliation["pending_before"] = len(pending_before)
    reconciliation["pending_after"] = len(state.pending_orders)
    return SessionResult(
        state=state,
        fills=fills,
        trades=trades,
        position_fill_links=links,
        equity_snapshot=equity,
        reconciliation=reconciliation,
        events=events,
    )


def process_session_close(
    state: PortfolioState,
    session_input: SessionInput | date,
    candidates: Iterable[Any] | None = None,
    market_data: dict[str, Any] | None = None,
    config: Any | None = None,
) -> SessionResult | PortfolioState:
    if isinstance(session_input, date):
        state.last_session_date = session_input
        state.last_completed_phase = "close"
        return state
    _ = candidates, market_data, config
    candidate_rows = list(session_input.candidates or session_input.signal_rows)
    pending_rows = _rows(session_input.execution_rows, "pending_rows")
    order_intents: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for row in pending_rows:
        pending_id = str(row.get("pending_order_id", ""))
        intent_id = str(row.get("order_intent_id", ""))
        if not pending_id:
            continue
        state.pending_orders[pending_id] = dict(row)
        order_intents.append(
            {
                "order_intent_id": intent_id,
                "candidate_id": row.get("candidate_id", ""),
                "session_date": session_input.session_date.isoformat(),
                **dict(row),
            }
        )
        events.append(
            {
                "event": "pending_order_created",
                "pending_order_id": pending_id,
                "order_intent_id": intent_id,
                "candidate_id": row.get("candidate_id", ""),
            }
        )
    equity = _equity_payload(state, session_input, None)
    state.cash = float(equity.get("cash", state.cash))
    state.previous_close_equity = float(equity.get("equity", state.previous_close_equity or state.cash))
    state.last_session_date = session_input.session_date
    state.last_completed_phase = "close"
    state.risk_state["last_close_data_snapshot_hash"] = session_input.data_snapshot_hash
    return SessionResult(
        state=state,
        candidates=candidate_rows,
        order_intents=order_intents,
        pending_orders=pending_rows,
        equity_snapshot=equity,
        reconciliation=_reconcile_state(state, equity),
        events=events,
    )


class PaperStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.create_schema()

    def close(self) -> None:
        self.conn.close()

    def create_schema(self) -> None:
        self.conn.executescript(
            """
            create table if not exists paper_runs(
                run_id text primary key,
                cohort_id text not null,
                strategy_commit text not null,
                full_config_hash text not null,
                audit_schema_version text not null,
                initial_cash real not null,
                payload_hash text not null,
                created_at text not null default current_timestamp
            );
            create table if not exists paper_sessions(
                run_id text not null,
                session_date text not null,
                phase text not null,
                status text not null,
                state_before_hash text not null,
                state_after_hash text not null,
                payload_hash text not null,
                snapshot_hash text not null,
                created_at text not null default current_timestamp,
                primary key(run_id, session_date, phase)
            );
            create table if not exists portfolio_state_snapshots(
                run_id text not null,
                session_date text not null,
                phase text not null,
                state_version text not null,
                state_before_hash text not null,
                state_after_hash text not null,
                cash real not null,
                positions_json text not null,
                pending_orders_json text not null,
                risk_state_json text not null,
                previous_close_equity real not null,
                payload_hash text not null,
                created_at text not null default current_timestamp,
                primary key(run_id, session_date, phase)
            );
            create table if not exists ledger_events(
                run_id text not null,
                event_id text not null,
                session_date text not null,
                phase text not null,
                payload_hash text not null,
                payload_json text not null,
                primary key(run_id, event_id)
            );
            create table if not exists candidate_snapshots(
                run_id text not null,
                session_date text not null,
                candidate_id text not null,
                selected integer not null,
                rank integer not null,
                payload_hash text not null,
                payload_json text not null,
                primary key(run_id, candidate_id)
            );
            create table if not exists order_intents(
                run_id text not null,
                order_intent_id text not null,
                session_date text not null,
                candidate_id text not null,
                payload_hash text not null,
                payload_json text not null,
                primary key(run_id, order_intent_id)
            );
            create table if not exists pending_orders(
                run_id text not null,
                pending_order_id text not null,
                session_date text not null,
                order_intent_id text not null,
                payload_hash text not null,
                payload_json text not null,
                primary key(run_id, pending_order_id)
            );
            create table if not exists fills(
                run_id text not null,
                fill_id text not null,
                session_date text not null,
                pending_order_id text not null,
                position_id text not null,
                trade_id text not null,
                payload_hash text not null,
                payload_json text not null,
                primary key(run_id, fill_id)
            );
            create table if not exists positions(
                run_id text not null,
                position_id text not null,
                session_date text not null,
                code text not null,
                shares integer not null,
                average_price real not null,
                payload_hash text not null,
                payload_json text not null,
                primary key(run_id, position_id, session_date)
            );
            create table if not exists position_fill_links(
                run_id text not null,
                position_id text not null,
                fill_id text not null,
                trade_id text not null,
                payload_hash text not null,
                payload_json text not null,
                primary key(run_id, position_id, fill_id)
            );
            create table if not exists trades(
                run_id text not null,
                trade_id text not null,
                session_date text not null,
                position_id text not null,
                exit_fill_id text not null,
                payload_hash text not null,
                payload_json text not null,
                primary key(run_id, trade_id)
            );
            create table if not exists equity_snapshots(
                run_id text not null,
                session_date text not null,
                cash real not null,
                equity real not null,
                exposure real not null,
                payload_hash text not null,
                payload_json text not null,
                primary key(run_id, session_date)
            );
            create table if not exists reconciliation_results(
                run_id text not null,
                session_date text not null,
                check_name text not null,
                status text not null,
                payload_hash text not null,
                details_json text not null,
                primary key(run_id, session_date, check_name)
            );
            create table if not exists data_snapshots(
                run_id text not null,
                session_date text not null,
                status text not null,
                snapshot_hash text not null,
                payload_hash text not null,
                details_json text not null,
                primary key(run_id, session_date)
            );
            """
        )

    def init_run(
        self,
        *,
        run_id: str,
        cohort_id: str,
        strategy_commit: str,
        full_config_hash: str,
        audit_schema_version: str,
        initial_cash: float,
    ) -> None:
        payload = {
            "run_id": run_id,
            "cohort_id": cohort_id,
            "strategy_commit": strategy_commit,
            "full_config_hash": full_config_hash,
            "audit_schema_version": audit_schema_version,
            "initial_cash": float(initial_cash),
        }
        payload_hash = stable_hash_json(payload)
        with self.conn:
            existing = self.conn.execute("select payload_hash from paper_runs where run_id = ?", (run_id,)).fetchone()
            if existing:
                if str(existing["payload_hash"]) != payload_hash:
                    raise IdempotencyConflict(f"paper_runs conflict for run_id={run_id}")
                return
            self.conn.execute(
                """
                insert into paper_runs(
                    run_id, cohort_id, strategy_commit, full_config_hash, audit_schema_version, initial_cash, payload_hash
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, cohort_id, strategy_commit, full_config_hash, audit_schema_version, initial_cash, payload_hash),
            )

    def load_latest_state(self, run_id: str) -> PortfolioState | None:
        row = self.conn.execute(
            """
            select * from portfolio_state_snapshots
            where run_id = ?
            order by session_date desc, case phase when 'open' then 1 when 'close' then 2 else 3 end desc
            limit 1
            """,
            (run_id,),
        ).fetchone()
        run = self.conn.execute("select * from paper_runs where run_id = ?", (run_id,)).fetchone()
        if row is None:
            if run is None:
                return None
            return PortfolioState(
                run_id=run_id,
                state_version=PAPER_STATE_VERSION,
                cash=float(run["initial_cash"]),
                previous_close_equity=float(run["initial_cash"]),
                strategy_commit=str(run["strategy_commit"]),
                full_config_hash=str(run["full_config_hash"]),
                audit_schema_version=str(run["audit_schema_version"]),
            )
        return PortfolioState(
            run_id=run_id,
            state_version=str(row["state_version"]),
            last_session_date=date.fromisoformat(str(row["session_date"])),
            last_completed_phase=str(row["phase"]),
            cash=float(row["cash"]),
            positions=json.loads(row["positions_json"]),
            pending_orders=json.loads(row["pending_orders_json"]),
            risk_state=json.loads(row["risk_state_json"]),
            previous_close_equity=float(row["previous_close_equity"]),
            strategy_commit=str(run["strategy_commit"]) if run else "",
            full_config_hash=str(run["full_config_hash"]) if run else "",
            audit_schema_version=str(run["audit_schema_version"]) if run else "",
        )

    def record_session(self, run_id: str, session_date: date, phase: str, status: str, payload: dict[str, Any]) -> None:
        before = payload.get("state_before_hash", "")
        after = payload.get("state_after_hash", payload.get("snapshot_hash", stable_hash_json(payload)))
        payload_hash = stable_hash_json({"status": status, "payload": payload})
        with self.conn:
            self._insert_idempotent(
                "paper_sessions",
                ("run_id", "session_date", "phase"),
                (run_id, session_date.isoformat(), phase),
                payload_hash,
                """
                insert into paper_sessions(
                    run_id, session_date, phase, status, state_before_hash, state_after_hash, payload_hash, snapshot_hash
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, session_date.isoformat(), phase, status, before, after, payload_hash, after),
            )

    def persist_session_result(
        self,
        run_id: str,
        session_date: date,
        phase: str,
        state_before: PortfolioState,
        result: SessionResult,
        status: str = "COMMITTED",
    ) -> None:
        state_before_hash = state_hash(state_before)
        state_after_hash = state_hash(result.state)
        payload = {
            "phase": phase,
            "status": status,
            "state_before_hash": state_before_hash,
            "state_after_hash": state_after_hash,
            "candidate_count": len(result.candidates),
            "order_intent_count": len(result.order_intents),
            "pending_count": len(result.pending_orders),
            "fill_count": len(result.fills),
            "trade_count": len(result.trades),
            "event_count": len(result.events),
            "reconciliation": result.reconciliation,
        }
        payload_hash = stable_hash_json(payload)
        self.conn.execute("begin immediate")
        try:
            existing = self.conn.execute(
                "select state_before_hash, payload_hash from paper_sessions where run_id = ? and session_date = ? and phase = ?",
                (run_id, session_date.isoformat(), phase),
            ).fetchone()
            if existing:
                if str(existing["state_before_hash"]) != state_before_hash or str(existing["payload_hash"]) != payload_hash:
                    raise IdempotencyConflict(f"paper_sessions conflict for {run_id} {session_date} {phase}")
                self.conn.rollback()
                return
            self.conn.execute(
                """
                insert into paper_sessions(
                    run_id, session_date, phase, status, state_before_hash, state_after_hash, payload_hash, snapshot_hash
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, session_date.isoformat(), phase, status, state_before_hash, state_after_hash, payload_hash, state_after_hash),
            )
            self._ingest_kernel_day_in_tx(
                run_id=run_id,
                session_date=session_date,
                candidate_rows=result.candidates,
                pending_rows=result.pending_orders,
                fill_rows=result.fills,
                trade_rows=result.trades,
                position_link_rows=result.position_fill_links,
                equity_payload=result.equity_snapshot,
            )
            for idx, event in enumerate(result.events):
                event_id = str(event.get("event_id") or stable_hash_json([session_date.isoformat(), phase, idx, event]))
                self._insert_payload_row(
                    "ledger_events",
                    ("run_id", "event_id"),
                    (run_id, event_id),
                    """
                    insert into ledger_events(run_id, event_id, session_date, phase, payload_hash, payload_json)
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, event_id, session_date.isoformat(), phase),
                    event,
                )
            self._insert_state_snapshot(run_id, session_date, phase, state_before_hash, state_after_hash, result.state, payload_hash)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def record_data_gate(self, run_id: str, session_date: date, status: str, details: dict[str, Any]) -> None:
        payload_hash = stable_hash_json({"status": status, "details": details})
        with self.conn:
            self._insert_idempotent(
                "data_snapshots",
                ("run_id", "session_date"),
                (run_id, session_date.isoformat()),
                payload_hash,
                """
                insert into data_snapshots(run_id, session_date, status, snapshot_hash, payload_hash, details_json)
                values (?, ?, ?, ?, ?, ?)
                """,
                (run_id, session_date.isoformat(), status, stable_hash_json(details), payload_hash, _json(details)),
            )

    def ingest_kernel_day(
        self,
        *,
        run_id: str,
        session_date: date,
        candidate_rows: list[dict[str, Any]],
        pending_rows: list[dict[str, Any]],
        fill_rows: list[dict[str, Any]],
        trade_rows: list[dict[str, Any]],
        position_link_rows: list[dict[str, Any]],
        equity_payload: dict[str, Any],
    ) -> None:
        with self.conn:
            self._ingest_kernel_day_in_tx(
                run_id=run_id,
                session_date=session_date,
                candidate_rows=candidate_rows,
                pending_rows=pending_rows,
                fill_rows=fill_rows,
                trade_rows=trade_rows,
                position_link_rows=position_link_rows,
                equity_payload=equity_payload,
            )

    def record_reconciliation(self, run_id: str, session_date: date, check_name: str, status: str, details: dict[str, Any]) -> None:
        payload_hash = stable_hash_json({"status": status, "details": details})
        with self.conn:
            self._insert_idempotent(
                "reconciliation_results",
                ("run_id", "session_date", "check_name"),
                (run_id, session_date.isoformat(), check_name),
                payload_hash,
                "insert into reconciliation_results values (?, ?, ?, ?, ?, ?)",
                (run_id, session_date.isoformat(), check_name, status, payload_hash, _json(details)),
            )

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"select count(*) from {table}").fetchone()[0])

    def duplicate_count(self, table: str, id_column: str, run_id: str) -> int:
        row = self.conn.execute(
            f"""
            select count(*) from (
                select {id_column}, count(*) c from {table}
                where run_id = ?
                group by {id_column}
                having c > 1
            )
            """,
            (run_id,),
        ).fetchone()
        return int(row[0])

    def orphan_count(self, run_id: str) -> int:
        return sum(self.orphan_breakdown(run_id).values())

    def orphan_breakdown(self, run_id: str) -> dict[str, int]:
        checks = {
            "order_intent_missing_candidate": """
                select count(*) from order_intents oi
                left join candidate_snapshots c on oi.run_id = c.run_id and oi.candidate_id = c.candidate_id
                where oi.run_id = ? and oi.candidate_id != '' and c.candidate_id is null
            """,
            "pending_missing_order_intent": """
                select count(*) from pending_orders p
                left join order_intents oi on p.run_id = oi.run_id and p.order_intent_id = oi.order_intent_id
                where p.run_id = ? and p.order_intent_id != '' and oi.order_intent_id is null
            """,
            "fill_missing_pending": """
                select count(*) from fills f
                left join pending_orders p on f.run_id = p.run_id and f.pending_order_id = p.pending_order_id
                where f.run_id = ? and f.pending_order_id != '' and p.pending_order_id is null
            """,
            "fill_missing_position": """
                select count(*) from fills f
                left join positions p on f.run_id = p.run_id and f.position_id = p.position_id and f.session_date = p.session_date
                where f.run_id = ? and f.position_id != '' and p.position_id is null
            """,
            "position_link_missing_fill": """
                select count(*) from position_fill_links l
                left join fills f on l.run_id = f.run_id and l.fill_id = f.fill_id
                where l.run_id = ? and f.fill_id is null
            """,
            "trade_missing_position": """
                select count(*) from trades t
                left join positions p on t.run_id = p.run_id and t.position_id = p.position_id
                where t.run_id = ? and t.position_id != '' and p.position_id is null
            """,
            "trade_missing_exit_fill": """
                select count(*) from trades t
                left join fills f on t.run_id = f.run_id and t.exit_fill_id = f.fill_id
                where t.run_id = ? and t.exit_fill_id != '' and f.fill_id is null
            """,
        }
        return {name: int(self.conn.execute(query, (run_id,)).fetchone()[0]) for name, query in checks.items()}

    def table_counts(self) -> dict[str, int]:
        tables = (
            "paper_runs",
            "paper_sessions",
            "portfolio_state_snapshots",
            "ledger_events",
            "candidate_snapshots",
            "order_intents",
            "pending_orders",
            "fills",
            "positions",
            "position_fill_links",
            "trades",
            "equity_snapshots",
            "reconciliation_results",
            "data_snapshots",
        )
        return {table: self.count(table) for table in tables}

    def _ingest_kernel_day_in_tx(
        self,
        *,
        run_id: str,
        session_date: date,
        candidate_rows: list[dict[str, Any]],
        pending_rows: list[dict[str, Any]],
        fill_rows: list[dict[str, Any]],
        trade_rows: list[dict[str, Any]],
        position_link_rows: list[dict[str, Any]],
        equity_payload: dict[str, Any],
    ) -> None:
        for row in candidate_rows:
            candidate_id = str(row.get("candidate_id", ""))
            if not candidate_id:
                continue
            self._insert_payload_row(
                "candidate_snapshots",
                ("run_id", "candidate_id"),
                (run_id, candidate_id),
                """
                insert into candidate_snapshots(
                    run_id, session_date, candidate_id, selected, rank, payload_hash, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_date.isoformat(),
                    candidate_id,
                    int(float(row.get("selected", 0) or 0)),
                    int(float(row.get("rank", 0) or 0)),
                ),
                row,
            )
        for row in pending_rows:
            pending_order_id = str(row.get("pending_order_id", ""))
            order_intent_id = str(row.get("order_intent_id", ""))
            if not pending_order_id:
                continue
            self._insert_payload_row(
                "order_intents",
                ("run_id", "order_intent_id"),
                (run_id, order_intent_id),
                "insert into order_intents values (?, ?, ?, ?, ?, ?)",
                (run_id, order_intent_id, session_date.isoformat(), str(row.get("candidate_id", ""))),
                row,
            )
            self._insert_payload_row(
                "pending_orders",
                ("run_id", "pending_order_id"),
                (run_id, pending_order_id),
                "insert into pending_orders values (?, ?, ?, ?, ?, ?)",
                (run_id, pending_order_id, session_date.isoformat(), order_intent_id),
                row,
            )
        position_state = self._position_state_before(run_id, session_date)
        for row in fill_rows:
            fill_id = str(row.get("fill_id", ""))
            if not fill_id:
                continue
            pending_id = _fill_pending_order_id(row)
            self._insert_payload_row(
                "fills",
                ("run_id", "fill_id"),
                (run_id, fill_id),
                "insert into fills values (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    fill_id,
                    session_date.isoformat(),
                    pending_id,
                    str(row.get("position_id", "")),
                    str(row.get("trade_id", "")),
                ),
                row,
            )
            position_id = str(row.get("position_id", ""))
            if position_id:
                position_payload = _position_snapshot_from_fill(position_state, row, session_date)
                self._insert_payload_row(
                    "positions",
                    ("run_id", "position_id", "session_date"),
                    (run_id, position_id, session_date.isoformat()),
                    "insert into positions values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        position_id,
                        session_date.isoformat(),
                        str(position_payload.get("code", "")),
                        int(position_payload.get("shares", 0)),
                        float(position_payload.get("average_price", 0.0)),
                    ),
                    position_payload,
                )
                position_state[position_id] = position_payload
        for row in position_link_rows:
            fill_id = str(row.get("fill_id", ""))
            position_id = str(row.get("position_id", ""))
            if not fill_id or not position_id:
                continue
            self._insert_payload_row(
                "position_fill_links",
                ("run_id", "position_id", "fill_id"),
                (run_id, position_id, fill_id),
                "insert into position_fill_links values (?, ?, ?, ?, ?, ?)",
                (run_id, position_id, fill_id, str(row.get("trade_id", ""))),
                row,
            )
        for row in trade_rows:
            trade_id = str(row.get("trade_id", ""))
            if not trade_id:
                continue
            self._insert_payload_row(
                "trades",
                ("run_id", "trade_id"),
                (run_id, trade_id),
                "insert into trades values (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    trade_id,
                    session_date.isoformat(),
                    str(row.get("position_id", "")),
                    str(row.get("exit_fill_id", "")),
                ),
                row,
            )
        if equity_payload:
            self._insert_payload_row(
                "equity_snapshots",
                ("run_id", "session_date"),
                (run_id, session_date.isoformat()),
                "insert into equity_snapshots values (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    session_date.isoformat(),
                    float(equity_payload.get("cash", 0.0)),
                    float(equity_payload.get("equity", 0.0)),
                    float(equity_payload.get("exposure", 0.0)),
                ),
                equity_payload,
            )

    def _insert_state_snapshot(
        self,
        run_id: str,
        session_date: date,
        phase: str,
        state_before_hash: str,
        state_after_hash: str,
        state: PortfolioState,
        payload_hash: str,
    ) -> None:
        self._insert_idempotent(
            "portfolio_state_snapshots",
            ("run_id", "session_date", "phase"),
            (run_id, session_date.isoformat(), phase),
            payload_hash,
            """
            insert into portfolio_state_snapshots(
                run_id, session_date, phase, state_version, state_before_hash, state_after_hash, cash,
                positions_json, pending_orders_json, risk_state_json, previous_close_equity, payload_hash
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                session_date.isoformat(),
                phase,
                state.state_version,
                state_before_hash,
                state_after_hash,
                float(state.cash),
                _json(state.positions),
                _json(state.pending_orders),
                _json(state.risk_state),
                float(state.previous_close_equity),
                payload_hash,
            ),
        )

    def _insert_payload_row(
        self,
        table: str,
        key_columns: tuple[str, ...],
        key_values: tuple[Any, ...],
        insert_sql: str,
        insert_prefix: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        payload_hash = stable_hash_json(payload)
        self._insert_idempotent(
            table,
            key_columns,
            key_values,
            payload_hash,
            insert_sql,
            (*insert_prefix, payload_hash, _json(payload)),
        )

    def _insert_idempotent(
        self,
        table: str,
        key_columns: tuple[str, ...],
        key_values: tuple[Any, ...],
        payload_hash: str,
        insert_sql: str,
        insert_values: tuple[Any, ...],
    ) -> None:
        where = " and ".join(f"{column} = ?" for column in key_columns)
        existing = self.conn.execute(f"select payload_hash from {table} where {where}", key_values).fetchone()
        if existing:
            if str(existing["payload_hash"]) != payload_hash:
                raise IdempotencyConflict(f"{table} conflict for {dict(zip(key_columns, key_values))}")
            return
        self.conn.execute(insert_sql, insert_values)

    def _position_state_before(self, run_id: str, before_date: date) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            """
            select p.* from positions p
            join (
                select position_id, max(session_date) as max_date
                from positions
                where run_id = ? and session_date < ?
                group by position_id
            ) latest on p.position_id = latest.position_id and p.session_date = latest.max_date
            where p.run_id = ?
            """,
            (run_id, before_date.isoformat(), run_id),
        ).fetchall()
        return {str(row["position_id"]): json.loads(row["payload_json"]) for row in rows}


def state_to_json(state: PortfolioState) -> str:
    return _json(state_payload(state))


def state_hash(state: PortfolioState) -> str:
    return stable_hash_json(state_payload(state))


def state_payload(state: PortfolioState) -> dict[str, Any]:
    payload = asdict(state)
    if state.last_session_date:
        payload["last_session_date"] = state.last_session_date.isoformat()
    payload.pop("last_processed_date", None)
    return payload


def _rows(container: dict[str, list[dict[str, Any]]], key: str) -> list[dict[str, Any]]:
    return [dict(row) for row in container.get(key, [])]


def _fill_pending_order_id(row: dict[str, Any]) -> str:
    return str(row.get("fill_pending_order_id") or row.get("pending_order_id") or "")


def _apply_fill_to_state(state: PortfolioState, fill: dict[str, Any]) -> None:
    position_id = str(fill.get("position_id", ""))
    if not position_id:
        return
    current = dict(state.positions.get(position_id, {}))
    updated = _position_snapshot_from_fill({position_id: current} if current else {}, fill, fill.get("date"))
    state.positions[position_id] = updated
    side = str(fill.get("side", ""))
    amount = float(fill.get("price", 0.0) or 0.0) * int(float(fill.get("shares", 0) or 0))
    fee = float(fill.get("fee", 0.0) or 0.0)
    if side in {"buy", "add"}:
        state.cash -= amount + fee
    else:
        state.cash += amount - fee


def _position_snapshot_from_fill(
    position_state: dict[str, dict[str, Any]],
    fill: dict[str, Any],
    session_date: date | str | None,
) -> dict[str, Any]:
    position_id = str(fill.get("position_id", ""))
    current = dict(position_state.get(position_id, {}))
    side = str(fill.get("side", ""))
    shares = int(float(fill.get("shares", 0) or 0))
    price = float(fill.get("price", 0.0) or 0.0)
    prior_shares = int(float(current.get("shares", 0) or 0))
    prior_avg = float(current.get("average_price", current.get("entry_price", 0.0)) or 0.0)
    if side in {"buy", "add"}:
        new_shares = prior_shares + shares
        average_price = ((prior_avg * prior_shares) + (price * shares)) / new_shares if new_shares else 0.0
        entry_costs = float(current.get("entry_costs", 0.0) or 0.0) + float(fill.get("fee", 0.0) or 0.0)
    else:
        new_shares = max(0, prior_shares - shares)
        average_price = prior_avg
        entry_costs = float(current.get("entry_costs", 0.0) or 0.0)
    snapshot = {
        **current,
        "position_id": position_id,
        "code": fill.get("code", current.get("code", "")),
        "shares": new_shares,
        "average_price": average_price,
        "entry_price": current.get("entry_price", price if side in {"buy", "add"} else prior_avg),
        "stop": fill.get("stop", current.get("stop", 0.0)),
        "target": fill.get("target", current.get("target", 0.0)),
        "highest_high": max(float(current.get("highest_high", 0.0) or 0.0), price),
        "pyramid_stage": int(float(current.get("pyramid_stage", 0) or 0)) + (1 if side == "add" else 0),
        "entry_costs": entry_costs,
        "risk_cash": current.get("risk_cash", fill.get("initial_risk_cash", 0.0)),
        "entry_date": current.get("entry_date") or str(fill.get("signal_date") or session_date or ""),
        "last_fill_id": fill.get("fill_id", ""),
        "last_fill_side": side,
    }
    return snapshot


def _equity_payload(state: PortfolioState, session_input: SessionInput, market_data: dict[str, Any] | None) -> dict[str, Any]:
    if session_input.equity_payload:
        return dict(session_input.equity_payload)
    if market_data and market_data.get("equity_payload"):
        return dict(market_data["equity_payload"])
    equity = float(state.cash)
    return {"cash": float(state.cash), "equity": equity, "exposure": 0.0}


def _reconcile_state(state: PortfolioState, equity: dict[str, Any]) -> dict[str, Any]:
    reported_cash = float(equity.get("cash", state.cash))
    reported_equity = float(equity.get("equity", state.previous_close_equity or reported_cash))
    return {
        "cash_match": abs(reported_cash - float(state.cash)) < 1e-6,
        "reported_cash": reported_cash,
        "state_cash": float(state.cash),
        "reported_equity": reported_equity,
        "position_count": sum(1 for row in state.positions.values() if int(float(row.get("shares", 0) or 0)) > 0),
        "pending_order_count": len(state.pending_orders),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

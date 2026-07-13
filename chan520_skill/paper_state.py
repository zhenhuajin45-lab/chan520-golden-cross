from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .evidence_manifest import stable_hash_json


PAPER_STATE_VERSION = "1"


@dataclass
class PortfolioState:
    run_id: str
    state_version: str
    last_processed_date: date | None
    cash: float
    positions: dict[str, Any] = field(default_factory=dict)
    pending_orders: dict[str, Any] = field(default_factory=dict)
    risk_state: dict[str, Any] = field(default_factory=dict)
    previous_close_equity: float = 0.0
    strategy_commit: str = ""
    full_config_hash: str = ""
    audit_schema_version: str = ""


def process_session_open(
    state: PortfolioState,
    session_date: date,
    market_data: dict[str, Any] | None = None,
    config: Any | None = None,
) -> PortfolioState:
    _ = market_data, config
    state.last_processed_date = session_date
    return state


def process_session_close(
    state: PortfolioState,
    session_date: date,
    candidates: Iterable[Any] | None = None,
    market_data: dict[str, Any] | None = None,
    config: Any | None = None,
) -> PortfolioState:
    _ = candidates, market_data, config
    state.last_processed_date = session_date
    return state


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
                created_at text not null default current_timestamp
            );
            create table if not exists paper_sessions(
                run_id text not null,
                session_date text not null,
                phase text not null,
                status text not null,
                snapshot_hash text not null,
                created_at text not null default current_timestamp,
                primary key(run_id, session_date, phase)
            );
            create table if not exists candidate_snapshots(
                run_id text not null,
                session_date text not null,
                candidate_id text not null,
                selected integer not null,
                rank integer not null,
                payload_json text not null,
                primary key(run_id, candidate_id)
            );
            create table if not exists order_intents(
                run_id text not null,
                order_intent_id text not null,
                session_date text not null,
                candidate_id text not null,
                payload_json text not null,
                primary key(run_id, order_intent_id)
            );
            create table if not exists pending_orders(
                run_id text not null,
                pending_order_id text not null,
                session_date text not null,
                order_intent_id text not null,
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
                payload_json text not null,
                primary key(run_id, fill_id)
            );
            create table if not exists positions(
                run_id text not null,
                position_id text not null,
                session_date text not null,
                code text not null,
                shares integer not null,
                payload_json text not null,
                primary key(run_id, position_id, session_date)
            );
            create table if not exists position_fill_links(
                run_id text not null,
                position_id text not null,
                fill_id text not null,
                trade_id text not null,
                payload_json text not null,
                primary key(run_id, position_id, fill_id)
            );
            create table if not exists trades(
                run_id text not null,
                trade_id text not null,
                session_date text not null,
                position_id text not null,
                payload_json text not null,
                primary key(run_id, trade_id)
            );
            create table if not exists equity_snapshots(
                run_id text not null,
                session_date text not null,
                cash real not null,
                equity real not null,
                exposure real not null,
                payload_json text not null,
                primary key(run_id, session_date)
            );
            create table if not exists reconciliation_results(
                run_id text not null,
                session_date text not null,
                check_name text not null,
                status text not null,
                details_json text not null,
                primary key(run_id, session_date, check_name)
            );
            create table if not exists data_snapshots(
                run_id text not null,
                session_date text not null,
                status text not null,
                snapshot_hash text not null,
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
        with self.conn:
            self.conn.execute(
                """
                insert or ignore into paper_runs(
                    run_id, cohort_id, strategy_commit, full_config_hash, audit_schema_version, initial_cash
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (run_id, cohort_id, strategy_commit, full_config_hash, audit_schema_version, initial_cash),
            )

    def record_session(self, run_id: str, session_date: date, phase: str, status: str, payload: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                """
                insert or ignore into paper_sessions(run_id, session_date, phase, status, snapshot_hash)
                values (?, ?, ?, ?, ?)
                """,
                (run_id, session_date.isoformat(), phase, status, stable_hash_json(payload)),
            )

    def record_data_gate(self, run_id: str, session_date: date, status: str, details: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                """
                insert or replace into data_snapshots(run_id, session_date, status, snapshot_hash, details_json)
                values (?, ?, ?, ?, ?)
                """,
                (run_id, session_date.isoformat(), status, stable_hash_json(details), _json(details)),
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
            for row in candidate_rows:
                candidate_id = str(row.get("candidate_id", ""))
                if not candidate_id:
                    continue
                self.conn.execute(
                    """
                    insert or ignore into candidate_snapshots(
                        run_id, session_date, candidate_id, selected, rank, payload_json
                    ) values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        session_date.isoformat(),
                        candidate_id,
                        int(float(row.get("selected", 0) or 0)),
                        int(float(row.get("rank", 0) or 0)),
                        _json(row),
                    ),
                )
            for row in pending_rows:
                pending_order_id = str(row.get("pending_order_id", ""))
                order_intent_id = str(row.get("order_intent_id", ""))
                if not pending_order_id:
                    continue
                self.conn.execute(
                    "insert or ignore into order_intents values (?, ?, ?, ?, ?)",
                    (run_id, order_intent_id, session_date.isoformat(), str(row.get("candidate_id", "")), _json(row)),
                )
                self.conn.execute(
                    "insert or ignore into pending_orders values (?, ?, ?, ?, ?)",
                    (run_id, pending_order_id, session_date.isoformat(), order_intent_id, _json(row)),
                )
            for row in fill_rows:
                fill_id = str(row.get("fill_id", ""))
                if not fill_id:
                    continue
                shares = int(float(row.get("shares", 0) or 0))
                position_shares = shares if str(row.get("side", "")) in {"buy", "add"} else 0
                self.conn.execute(
                    "insert or ignore into fills values (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        fill_id,
                        session_date.isoformat(),
                        str(row.get("pending_order_id") or row.get("fill_pending_order_id") or ""),
                        str(row.get("position_id", "")),
                        str(row.get("trade_id", "")),
                        _json(row),
                    ),
                )
                if row.get("position_id"):
                    self.conn.execute(
                        "insert or ignore into positions values (?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            str(row.get("position_id", "")),
                            session_date.isoformat(),
                            str(row.get("code", "")),
                            position_shares,
                            _json(row),
                        ),
                    )
            for row in position_link_rows:
                fill_id = str(row.get("fill_id", ""))
                position_id = str(row.get("position_id", ""))
                if not fill_id or not position_id:
                    continue
                self.conn.execute(
                    "insert or ignore into position_fill_links values (?, ?, ?, ?, ?)",
                    (run_id, position_id, fill_id, str(row.get("trade_id", "")), _json(row)),
                )
            for row in trade_rows:
                trade_id = str(row.get("trade_id", ""))
                if not trade_id:
                    continue
                self.conn.execute(
                    "insert or ignore into trades values (?, ?, ?, ?, ?)",
                    (run_id, trade_id, session_date.isoformat(), str(row.get("position_id", "")), _json(row)),
                )
            self.conn.execute(
                """
                insert or replace into equity_snapshots(run_id, session_date, cash, equity, exposure, payload_json)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_date.isoformat(),
                    float(equity_payload.get("cash", 0.0)),
                    float(equity_payload.get("equity", 0.0)),
                    float(equity_payload.get("exposure", 0.0)),
                    _json(equity_payload),
                ),
            )

    def record_reconciliation(self, run_id: str, session_date: date, check_name: str, status: str, details: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "insert or replace into reconciliation_results values (?, ?, ?, ?, ?)",
                (run_id, session_date.isoformat(), check_name, status, _json(details)),
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
        checks = [
            """
            select count(*) from fills f
            left join pending_orders p on f.run_id = p.run_id and f.pending_order_id = p.pending_order_id
            where f.run_id = ? and f.pending_order_id != '' and p.pending_order_id is null
            """,
            """
            select count(*) from trades t
            left join fills f on t.run_id = f.run_id and t.trade_id = f.trade_id
            where t.run_id = ? and f.fill_id is null
            """,
        ]
        return sum(int(self.conn.execute(query, (run_id,)).fetchone()[0]) for query in checks)

    def table_counts(self) -> dict[str, int]:
        tables = (
            "paper_runs",
            "paper_sessions",
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


def state_to_json(state: PortfolioState) -> str:
    payload = asdict(state)
    if state.last_processed_date:
        payload["last_processed_date"] = state.last_processed_date.isoformat()
    return _json(payload)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

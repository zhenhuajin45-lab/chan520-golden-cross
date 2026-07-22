from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .broker_adapter import (
    BrokerOrderRequest,
    BrokerPreflight,
    BrokerSide,
    GMSimBrokerAdapter,
    GMSimulationConfig,
)


class LiveExecutionBlocked(RuntimeError):
    """Raised when the live/simulation execution path must fail closed."""


@dataclass(frozen=True)
class PendingExecutionPlan:
    execution_date: date
    decision_date: date
    run_id: str
    pending_rows: tuple[dict[str, Any], ...]
    data_max_dates: dict[str, str]

    @property
    def ready(self) -> bool:
        return bool(self.pending_rows)


def load_local_gm_sim_config(path: Path) -> GMSimulationConfig:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return GMSimulationConfig(
        account_id=str(payload.get("account_id", "") or ""),
        account_type=str(payload.get("account_type", "SIMULATION") or "SIMULATION"),
        token_env=str(payload.get("token_env", "GM_TOKEN") or "GM_TOKEN"),
        enable_submit=bool(payload.get("enable_submit", False)),
        dry_run=bool(payload.get("dry_run", True)),
    )


def build_pending_execution_plan(
    *,
    alpha_store: Path,
    paper_store: Path,
    run_id: str,
    execution_date: date,
    decision_date: date,
) -> PendingExecutionPlan:
    data_max_dates = max_store_dates(alpha_store)
    stale = {table: max_date for table, max_date in data_max_dates.items() if max_date and max_date < decision_date.isoformat()}
    if stale:
        raise LiveExecutionBlocked(f"GM alpha store stale for decision_date={decision_date.isoformat()}: {stale}")
    rows = pending_rows_for_decision_date(paper_store, run_id, decision_date)
    if not rows:
        raise LiveExecutionBlocked(f"no pending orders for run_id={run_id} decision_date={decision_date.isoformat()}")
    return PendingExecutionPlan(
        execution_date=execution_date,
        decision_date=decision_date,
        run_id=run_id,
        pending_rows=tuple(rows),
        data_max_dates=data_max_dates,
    )


def max_store_dates(alpha_store: Path) -> dict[str, str]:
    conn = sqlite3.connect(alpha_store)
    try:
        out: dict[str, str] = {}
        for table in ("daily_bars", "dynamic_universe", "index_bars", "instrument_status"):
            try:
                row = conn.execute(f"select trade_date from {table} order by trade_date desc limit 1").fetchone()
            except sqlite3.Error:
                row = None
            out[table] = str(row[0]) if row and row[0] else ""
        return out
    finally:
        conn.close()


def pending_rows_for_decision_date(paper_store: Path, run_id: str, decision_date: date) -> list[dict[str, Any]]:
    conn = sqlite3.connect(paper_store)
    conn.row_factory = sqlite3.Row
    try:
        rows = []
        for row in conn.execute(
            """
            select payload_json
            from pending_orders
            where run_id = ? and session_date = ?
            order by pending_order_id
            """,
            (run_id, decision_date.isoformat()),
        ):
            payload = json.loads(row["payload_json"])
            if str(payload.get("decision_date", decision_date.isoformat())) != decision_date.isoformat():
                continue
            if int(float(payload.get("shares", 0) or 0)) <= 0:
                continue
            rows.append(payload)
        return rows
    finally:
        conn.close()


def pending_row_to_broker_request(row: dict[str, Any], *, execution_date: date, run_id: str) -> BrokerOrderRequest:
    code = str(row.get("code", "") or "").strip()
    side_text = str(row.get("side", "") or "").strip().lower()
    shares = int(float(row.get("shares", 0) or 0))
    price = float(row.get("limit_price", row.get("signal_close", 0.0)) or 0.0)
    if not code or shares <= 0 or price <= 0:
        raise LiveExecutionBlocked(f"invalid pending order fields pending_order_id={row.get('pending_order_id', '')}")
    if side_text in {"buy", "add"}:
        side = BrokerSide.BUY
        position_effect = "OPEN"
    elif side_text == "sell":
        side = BrokerSide.SELL
        position_effect = "CLOSE"
    else:
        raise LiveExecutionBlocked(f"unsupported pending side={side_text}")
    return BrokerOrderRequest(
        symbol=to_gm_symbol(code),
        side=side,
        volume=shares,
        price=price,
        position_effect=position_effect,
        order_intent_id=str(row.get("order_intent_id", "") or ""),
        run_id=run_id,
        session_date=execution_date.isoformat(),
        client_order_id=str(row.get("pending_order_id", "") or ""),
        extra={
            "pending_order_id": str(row.get("pending_order_id", "") or ""),
            "candidate_id": str(row.get("candidate_id", "") or ""),
            "decision_date": str(row.get("decision_date", "") or ""),
            "signal_close": float(row.get("signal_close", 0.0) or 0.0),
            "planned_stop": float(row.get("planned_stop", 0.0) or 0.0),
            "planned_target": float(row.get("planned_target", 0.0) or 0.0),
            "rr": float(row.get("rr", 0.0) or 0.0),
            "reason": str(row.get("reason", "") or ""),
        },
    )


def execute_pending_plan(
    *,
    plan: PendingExecutionPlan,
    config: GMSimulationConfig,
    confirm_submit: bool,
    account_query_ok: bool,
) -> list[dict[str, Any]]:
    preflight = BrokerPreflight(
        identity_pass=True,
        data_gate_pass=True,
        reconciliation_pass=bool(account_query_ok),
    )
    effective_config = config
    if not confirm_submit:
        effective_config = GMSimulationConfig(
            account_id=config.account_id,
            account_type=config.account_type,
            token_env=config.token_env,
            enable_submit=False,
            dry_run=True,
        )
    adapter = GMSimBrokerAdapter(effective_config)
    results = []
    for row in plan.pending_rows:
        request = pending_row_to_broker_request(row, execution_date=plan.execution_date, run_id=plan.run_id)
        result = adapter.submit_order(request, preflight)
        results.append(
            {
                "pending_order_id": request.client_order_id,
                "symbol": request.symbol,
                "side": request.side.value,
                "volume": request.volume,
                "price": request.price,
                "result": result.as_payload(),
            }
        )
    return results


def to_gm_symbol(code: str) -> str:
    code = code.strip()
    if code.startswith(("SHSE.", "SZSE.")):
        return code
    if code.startswith(("6", "5", "9")):
        return f"SHSE.{code}"
    return f"SZSE.{code}"

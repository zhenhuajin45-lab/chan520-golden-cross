from __future__ import annotations

import os
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol


class BrokerSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class BrokerGuardCode(str, Enum):
    OK = "OK"
    PAPER_ACCEPTED = "PAPER_ACCEPTED"
    DRY_RUN = "DRY_RUN"
    SUBMIT_DISABLED = "SUBMIT_DISABLED"
    NON_SIM_ACCOUNT_TYPE = "NON_SIM_ACCOUNT_TYPE"
    ACCOUNT_ID_REQUIRED = "ACCOUNT_ID_REQUIRED"
    TOKEN_REQUIRED = "TOKEN_REQUIRED"
    IDENTITY_GATE_FAILED = "IDENTITY_GATE_FAILED"
    DATA_GATE_FAILED = "DATA_GATE_FAILED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    INVALID_ORDER = "INVALID_ORDER"
    INVALID_LOT_SIZE = "INVALID_LOT_SIZE"
    SESSION_DATE_REQUIRED = "SESSION_DATE_REQUIRED"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    T_PLUS_ONE_BLOCKED = "T_PLUS_ONE_BLOCKED"
    SAME_DAY_RISK_REENTRY_BLOCKED = "SAME_DAY_RISK_REENTRY_BLOCKED"
    PREVIOUS_SESSION_RISK_REENTRY_BLOCKED = "PREVIOUS_SESSION_RISK_REENTRY_BLOCKED"
    LOSS_ADD_BLOCKED = "LOSS_ADD_BLOCKED"
    LOCAL_LEDGER_ERROR = "LOCAL_LEDGER_ERROR"
    GM_UNAVAILABLE = "GM_UNAVAILABLE"
    GM_SUBMIT_EXCEPTION = "GM_SUBMIT_EXCEPTION"


class BrokerAdapterError(RuntimeError):
    """Raised for broker adapter integration defects, not market rejects."""


class GmClientProtocol(Protocol):
    def order_volume(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class BrokerPreflight:
    identity_pass: bool = False
    data_gate_pass: bool = False
    reconciliation_pass: bool = False
    message: str = ""

    def first_failure(self) -> BrokerGuardCode:
        if not self.identity_pass:
            return BrokerGuardCode.IDENTITY_GATE_FAILED
        if not self.data_gate_pass:
            return BrokerGuardCode.DATA_GATE_FAILED
        if not self.reconciliation_pass:
            return BrokerGuardCode.RECONCILIATION_FAILED
        return BrokerGuardCode.OK


@dataclass(frozen=True)
class BrokerOrderRequest:
    symbol: str
    side: BrokerSide
    volume: int
    price: float
    order_type: str = "LIMIT"
    position_effect: str = "OPEN"
    order_intent_id: str = ""
    run_id: str = ""
    session_date: str = ""
    client_order_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> BrokerGuardCode:
        if not self.symbol.strip():
            return BrokerGuardCode.INVALID_ORDER
        if self.volume <= 0:
            return BrokerGuardCode.INVALID_ORDER
        if self.price <= 0:
            return BrokerGuardCode.INVALID_ORDER
        if self.side not in {BrokerSide.BUY, BrokerSide.SELL}:
            return BrokerGuardCode.INVALID_ORDER
        return BrokerGuardCode.OK

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["side"] = self.side.value
        return payload


@dataclass(frozen=True)
class BrokerOrderResult:
    adapter: str
    submitted: bool
    accepted: bool
    reason_code: BrokerGuardCode
    order_id: str = ""
    cl_ord_id: str = ""
    counter_order_id: str = ""
    account_id_present: bool = False
    message: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    raw_order_repr: str = ""

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_code"] = self.reason_code.value
        return payload


@dataclass(frozen=True)
class GMSimulationConfig:
    account_id: str = ""
    account_type: str = "SIMULATION"
    token_env: str = "GM_TOKEN"
    enable_submit: bool = False
    dry_run: bool = True
    require_identity_gate: bool = True
    require_data_gate: bool = True
    require_reconciliation: bool = True


@dataclass(frozen=True)
class LocalSimBrokerConfig:
    account_id: str = "local-sim"
    initial_cash: float = 1_000_000.0
    ledger_path: str = "data/local_sim/broker.sqlite"
    lot_size: int = 100
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    transfer_rate: float = 0.00001
    cost_policy_id: str = "a_share_backtest_aligned_v1"


class InternalPaperBroker:
    name = "internal_paper"

    def submit_order(self, request: BrokerOrderRequest, preflight: BrokerPreflight | None = None) -> BrokerOrderResult:
        reason = request.validate()
        if reason is not BrokerGuardCode.OK:
            return BrokerOrderResult(
                adapter=self.name,
                submitted=False,
                accepted=False,
                reason_code=reason,
                request=request.as_payload(),
            )
        return BrokerOrderResult(
            adapter=self.name,
            submitted=False,
            accepted=True,
            reason_code=BrokerGuardCode.PAPER_ACCEPTED,
            request=request.as_payload(),
        )


class LocalSimBrokerAdapter:
    """Local SQLite simulated broker for offline paper trading.

    The adapter fills accepted limit orders immediately at the requested
    price. It is intentionally simple and deterministic so local paper
    trading can be audited without any external broker or GM dependency.
    """

    name = "local_simulation"

    def __init__(self, config: LocalSimBrokerConfig | None = None) -> None:
        self.config = config or LocalSimBrokerConfig()
        self.path = Path(self.config.ledger_path)

    def initialize_account(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            self._create_schema(conn)
            existing = conn.execute("select account_id from accounts where account_id = ?", (self.config.account_id,)).fetchone()
            if existing:
                return
            conn.execute(
                """
                insert into accounts(account_id, initial_cash, cash, created_at, updated_at)
                values (?, ?, ?, ?, ?)
                """,
                (
                    self.config.account_id,
                    float(self.config.initial_cash),
                    float(self.config.initial_cash),
                    _utc_now(),
                    _utc_now(),
                ),
            )

    def submit_order(self, request: BrokerOrderRequest, preflight: BrokerPreflight | None = None) -> BrokerOrderResult:
        _ = preflight
        reason = request.validate()
        if reason is not BrokerGuardCode.OK:
            return self._rejected(request, reason)
        if not request.session_date.strip():
            return self._rejected(request, BrokerGuardCode.SESSION_DATE_REQUIRED, "local simulation orders require session_date for audit and T+1 enforcement")
        lot_reason = self._validate_lot(request)
        if lot_reason is not BrokerGuardCode.OK:
            return self._rejected(request, lot_reason)
        try:
            self.initialize_account()
            with sqlite3.connect(self.path) as conn:
                conn.row_factory = sqlite3.Row
                existing = self._existing_client_order(conn, request.client_order_id)
                if existing is not None:
                    return self._existing_result(request, existing)
                return self._submit_order_tx(conn, request)
        except sqlite3.Error as exc:
            return self._rejected(request, BrokerGuardCode.LOCAL_LEDGER_ERROR, f"{type(exc).__name__}: {exc}")

    def account_snapshot(self) -> dict[str, Any]:
        self.initialize_account()
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            account = conn.execute("select * from accounts where account_id = ?", (self.config.account_id,)).fetchone()
            positions = conn.execute(
                "select symbol, shares, average_price from positions where account_id = ? and shares != 0 order by symbol",
                (self.config.account_id,),
            ).fetchall()
        return {
            "adapter": self.name,
            "account_id": self.config.account_id,
            "initial_cash": float(account["initial_cash"]),
            "cash": float(account["cash"]),
            "positions": [
                {
                    "symbol": str(row["symbol"]),
                    "shares": int(row["shares"]),
                    "average_price": float(row["average_price"]),
                }
                for row in positions
            ],
        }

    def record_planned_order(self, payload: dict[str, Any]) -> str:
        self.initialize_account()
        planned_order_id = str(payload.get("planned_order_id") or payload.get("pending_order_id") or f"PLAN-{uuid.uuid4().hex[:16]}")
        now = _utc_now()
        status = str(payload.get("status") or "PLANNED")
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            self._create_schema(conn)
            existing = conn.execute(
                """
                select status, payload_json, last_message, quote_json
                from planned_orders
                where account_id = ? and planned_order_id = ?
                """,
                (self.config.account_id, planned_order_id),
            ).fetchone()
            normalized = {**payload, "planned_order_id": planned_order_id, "status": status}
            existing_status = str(existing["status"]) if existing is not None else ""
            if existing is not None and existing_status == "RISK_CONFIRMED" and status == "RISK_CANDIDATE":
                status = "RISK_CONFIRMED"
                normalized["status"] = status
            payload_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
            if existing is not None:
                reset_risk_evidence = status == "RISK_CANDIDATE"
                stale_risk_evidence = reset_risk_evidence and (
                    bool(str(existing["last_message"] or ""))
                    or str(existing["quote_json"] or "{}") != "{}"
                )
                if str(existing["payload_json"]) != payload_json or stale_risk_evidence:
                    conn.execute(
                        """
                        update planned_orders
                        set pending_order_id = ?, order_intent_id = ?, run_id = ?,
                            trade_date = ?, symbol = ?, side = ?, volume = ?, status = ?,
                            trigger_price = ?, lower_price = ?, upper_price = ?,
                            stop_price = ?, target_price = ?, invalid_price = ?,
                            reason_code = ?, reason_text = ?, payload_json = ?,
                            last_message = ?, quote_json = ?, updated_at = ?
                        where account_id = ? and planned_order_id = ?
                        """,
                        (
                            str(normalized.get("pending_order_id") or ""),
                            str(normalized.get("order_intent_id") or ""),
                            str(normalized.get("run_id") or ""),
                            str(normalized.get("trade_date") or normalized.get("session_date") or ""),
                            str(normalized.get("symbol") or normalized.get("code") or ""),
                            str(normalized.get("side") or "").upper(),
                            int(float(normalized.get("volume", normalized.get("shares", 0)) or 0)),
                            status,
                            float(normalized.get("trigger_price", normalized.get("signal_close", 0.0)) or 0.0),
                            float(normalized.get("lower_price", normalized.get("entry_lower", 0.0)) or 0.0),
                            float(normalized.get("upper_price", normalized.get("entry_upper", 0.0)) or 0.0),
                            float(normalized.get("stop_price", normalized.get("planned_stop", normalized.get("stop", 0.0))) or 0.0),
                            float(normalized.get("target_price", normalized.get("planned_target", normalized.get("target", 0.0))) or 0.0),
                            float(normalized.get("invalid_price", normalized.get("stop", 0.0)) or 0.0),
                            str(normalized.get("reason_code") or ""),
                            str(normalized.get("reason_text") or normalized.get("reason") or ""),
                            payload_json,
                            "" if reset_risk_evidence else str(existing["last_message"] or ""),
                            "{}" if reset_risk_evidence else str(existing["quote_json"] or "{}"),
                            now,
                            self.config.account_id,
                            planned_order_id,
                        ),
                    )
                return planned_order_id
            conn.execute(
                """
                insert into planned_orders(
                    account_id, planned_order_id, pending_order_id, order_intent_id, run_id,
                    trade_date, symbol, side, volume, status, trigger_price, lower_price,
                    upper_price, stop_price, target_price, invalid_price, reason_code,
                    reason_text, payload_json, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.config.account_id,
                    planned_order_id,
                    str(normalized.get("pending_order_id") or ""),
                    str(normalized.get("order_intent_id") or ""),
                    str(normalized.get("run_id") or ""),
                    str(normalized.get("trade_date") or normalized.get("session_date") or ""),
                    str(normalized.get("symbol") or normalized.get("code") or ""),
                    str(normalized.get("side") or "").upper(),
                    int(float(normalized.get("volume", normalized.get("shares", 0)) or 0)),
                    status,
                    float(normalized.get("trigger_price", normalized.get("signal_close", 0.0)) or 0.0),
                    float(normalized.get("lower_price", normalized.get("entry_lower", 0.0)) or 0.0),
                    float(normalized.get("upper_price", normalized.get("entry_upper", 0.0)) or 0.0),
                    float(normalized.get("stop_price", normalized.get("planned_stop", normalized.get("stop", 0.0))) or 0.0),
                    float(normalized.get("target_price", normalized.get("planned_target", normalized.get("target", 0.0))) or 0.0),
                    float(normalized.get("invalid_price", normalized.get("stop", 0.0)) or 0.0),
                    str(normalized.get("reason_code") or ""),
                    str(normalized.get("reason_text") or normalized.get("reason") or ""),
                    payload_json,
                    now,
                    now,
                ),
            )
        return planned_order_id

    def mark_planned_order(self, planned_order_id: str, status: str, message: str = "", quote: dict[str, Any] | None = None) -> None:
        self.initialize_account()
        now = _utc_now()
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            self._create_schema(conn)
            conn.execute(
                """
                update planned_orders
                set status = ?, last_message = ?, quote_json = ?, updated_at = ?
                where account_id = ? and planned_order_id = ?
                """,
                (
                    status,
                    message,
                    json.dumps(quote or {}, ensure_ascii=False, sort_keys=True, default=str),
                    now,
                    self.config.account_id,
                    planned_order_id,
                ),
            )

    def planned_orders_snapshot(self, limit: int = 500) -> list[dict[str, Any]]:
        self.initialize_account()
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select *
                from planned_orders
                where account_id = ?
                order by created_at desc, planned_order_id desc
                limit ?
                """,
                (self.config.account_id, int(limit)),
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def _submit_order_tx(self, conn: sqlite3.Connection, request: BrokerOrderRequest) -> BrokerOrderResult:
        account = conn.execute("select cash from accounts where account_id = ?", (self.config.account_id,)).fetchone()
        if account is None:
            raise sqlite3.OperationalError(f"account missing: {self.config.account_id}")
        cash = float(account["cash"])
        position = conn.execute(
            "select shares, average_price from positions where account_id = ? and symbol = ?",
            (self.config.account_id, request.symbol),
        ).fetchone()
        shares = int(position["shares"]) if position is not None else 0
        average_price = float(position["average_price"]) if position is not None else 0.0
        gross = float(request.price) * int(request.volume)
        commission = self._commission(gross)
        stamp_duty = gross * self.config.stamp_duty_rate if request.side is BrokerSide.SELL else 0.0
        transfer_fee = gross * self.config.transfer_rate if request.symbol.upper().startswith("SHSE.") else 0.0
        fees = commission + stamp_duty + transfer_fee
        if request.side is BrokerSide.BUY:
            if self._has_same_day_sell(conn, request.symbol, request.session_date):
                return self._rejected(request, BrokerGuardCode.SAME_DAY_RISK_REENTRY_BLOCKED)
            previous_session = _extra_text(request, "previous_session_date")
            if previous_session and self._has_risk_sell(conn, request.symbol, previous_session):
                return self._rejected(request, BrokerGuardCode.PREVIOUS_SESSION_RISK_REENTRY_BLOCKED)
            if shares > 0 and float(request.price) < average_price:
                return self._rejected(request, BrokerGuardCode.LOSS_ADD_BLOCKED)
            total_cost = gross + fees
            if cash + 1e-9 < total_cost:
                return self._rejected(request, BrokerGuardCode.INSUFFICIENT_CASH)
            new_cash = cash - total_cost
            new_shares = shares + int(request.volume)
            new_average = ((average_price * shares) + gross) / new_shares
        else:
            if shares < int(request.volume):
                return self._rejected(request, BrokerGuardCode.INSUFFICIENT_POSITION)
            if request.session_date and self._sellable_shares(conn, request.symbol, request.session_date) < int(request.volume):
                return self._rejected(request, BrokerGuardCode.T_PLUS_ONE_BLOCKED)
            new_cash = cash + gross - fees
            new_shares = shares - int(request.volume)
            new_average = average_price if new_shares else 0.0
        order_id = f"LSIM-{uuid.uuid4().hex[:16]}"
        fill_id = f"FILL-{uuid.uuid4().hex[:16]}"
        now = _utc_now()
        conn.execute(
            """
            insert into orders(
                account_id, order_id, client_order_id, symbol, side, volume, price, status,
                order_intent_id, run_id, session_date, signal_name, entry_reason, exit_reason,
                risk_reason, risk_reason_code, notes, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.config.account_id,
                order_id,
                request.client_order_id,
                request.symbol,
                request.side.value,
                int(request.volume),
                float(request.price),
                "FILLED",
                request.order_intent_id,
                request.run_id,
                request.session_date,
                _extra_text(request, "signal_name"),
                _extra_text(request, "entry_reason"),
                _extra_text(request, "exit_reason"),
                _extra_text(request, "risk_reason"),
                _extra_text(request, "risk_reason_code"),
                _extra_text(request, "notes"),
                now,
            ),
        )
        conn.execute(
            """
            insert into fills(
                account_id, fill_id, order_id, symbol, side, volume, price, gross,
                commission, stamp_duty, transfer_fee, cost_policy_id, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.config.account_id,
                fill_id,
                order_id,
                request.symbol,
                request.side.value,
                int(request.volume),
                float(request.price),
                gross,
                commission,
                stamp_duty,
                transfer_fee,
                self.config.cost_policy_id,
                now,
            ),
        )
        if request.side is BrokerSide.BUY:
            self._record_buy_lot(conn, request)
        else:
            self._consume_sell_lots(conn, request)
        conn.execute(
            "update accounts set cash = ?, updated_at = ? where account_id = ?",
            (new_cash, now, self.config.account_id),
        )
        conn.execute(
            """
            insert into positions(account_id, symbol, shares, average_price, updated_at)
            values (?, ?, ?, ?, ?)
            on conflict(account_id, symbol) do update set
                shares = excluded.shares,
                average_price = excluded.average_price,
                updated_at = excluded.updated_at
            """,
            (self.config.account_id, request.symbol, new_shares, new_average, now),
        )
        return BrokerOrderResult(
            adapter=self.name,
            submitted=True,
            accepted=True,
            reason_code=BrokerGuardCode.OK,
            order_id=order_id,
            cl_ord_id=request.client_order_id,
            account_id_present=True,
            request=request.as_payload(),
            raw_order_repr=f"fill_id={fill_id}",
        )

    def _validate_lot(self, request: BrokerOrderRequest) -> BrokerGuardCode:
        lot_size = int(self.config.lot_size)
        if lot_size > 0 and int(request.volume) % lot_size != 0:
            return BrokerGuardCode.INVALID_LOT_SIZE
        return BrokerGuardCode.OK

    def _commission(self, gross: float) -> float:
        return max(gross * float(self.config.commission_rate), float(self.config.min_commission))

    def _existing_client_order(self, conn: sqlite3.Connection, client_order_id: str) -> sqlite3.Row | None:
        if not client_order_id:
            return None
        return conn.execute(
            """
            select order_id, client_order_id, symbol, side, volume, price, status
            from orders
            where account_id = ? and client_order_id = ?
            """,
            (self.config.account_id, client_order_id),
        ).fetchone()

    def _existing_result(self, request: BrokerOrderRequest, row: sqlite3.Row) -> BrokerOrderResult:
        if (
            str(row["symbol"]) != request.symbol
            or str(row["side"]) != request.side.value
            or int(row["volume"]) != int(request.volume)
            or abs(float(row["price"]) - float(request.price)) > 1e-9
        ):
            return self._rejected(request, BrokerGuardCode.LOCAL_LEDGER_ERROR, "client_order_id conflict")
        return BrokerOrderResult(
            adapter=self.name,
            submitted=True,
            accepted=str(row["status"]) == "FILLED",
            reason_code=BrokerGuardCode.OK,
            order_id=str(row["order_id"]),
            cl_ord_id=str(row["client_order_id"]),
            account_id_present=True,
            message="idempotent replay",
            request=request.as_payload(),
        )

    def _rejected(
        self,
        request: BrokerOrderRequest,
        reason: BrokerGuardCode,
        message: str = "",
    ) -> BrokerOrderResult:
        return BrokerOrderResult(
            adapter=self.name,
            submitted=False,
            accepted=False,
            reason_code=reason,
            account_id_present=bool(self.config.account_id.strip()),
            message=message,
            request=request.as_payload(),
        )

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            create table if not exists accounts(
                account_id text primary key,
                initial_cash real not null,
                cash real not null,
                created_at text not null,
                updated_at text not null
            );
            create table if not exists positions(
                account_id text not null,
                symbol text not null,
                shares integer not null,
                average_price real not null,
                updated_at text not null,
                primary key(account_id, symbol)
            );
            create table if not exists position_lots(
                account_id text not null,
                symbol text not null,
                lot_date text not null,
                shares integer not null,
                primary key(account_id, symbol, lot_date)
            );
            create table if not exists orders(
                account_id text not null,
                order_id text not null,
                client_order_id text not null default '',
                symbol text not null,
                side text not null,
                volume integer not null,
                price real not null,
                status text not null,
                order_intent_id text not null default '',
                run_id text not null default '',
                session_date text not null default '',
                signal_name text not null default '',
                entry_reason text not null default '',
                exit_reason text not null default '',
                risk_reason text not null default '',
                risk_reason_code text not null default '',
                notes text not null default '',
                created_at text not null,
                primary key(account_id, order_id)
            );
            create unique index if not exists idx_local_sim_orders_client_id
            on orders(account_id, client_order_id)
            where client_order_id != '';
            create table if not exists fills(
                account_id text not null,
                fill_id text not null,
                order_id text not null,
                symbol text not null,
                side text not null,
                volume integer not null,
                price real not null,
                gross real not null,
                commission real not null,
                stamp_duty real not null,
                transfer_fee real not null default 0,
                cost_policy_id text not null default '',
                created_at text not null,
                primary key(account_id, fill_id)
            );
            create table if not exists planned_orders(
                account_id text not null,
                planned_order_id text not null,
                pending_order_id text not null default '',
                order_intent_id text not null default '',
                run_id text not null default '',
                trade_date text not null default '',
                symbol text not null,
                side text not null,
                volume integer not null default 0,
                status text not null,
                trigger_price real not null default 0,
                lower_price real not null default 0,
                upper_price real not null default 0,
                stop_price real not null default 0,
                target_price real not null default 0,
                invalid_price real not null default 0,
                reason_code text not null default '',
                reason_text text not null default '',
                last_message text not null default '',
                quote_json text not null default '{}',
                payload_json text not null,
                created_at text not null,
                updated_at text not null,
                primary key(account_id, planned_order_id)
            );
            """
        )
        self._ensure_columns(
            conn,
            "orders",
            {
                "signal_name": "text not null default ''",
                "entry_reason": "text not null default ''",
                "exit_reason": "text not null default ''",
                "risk_reason": "text not null default ''",
                "risk_reason_code": "text not null default ''",
                "notes": "text not null default ''",
            },
        )
        self._ensure_columns(
            conn,
            "fills",
            {
                "transfer_fee": "real not null default 0",
                "cost_policy_id": "text not null default ''",
            },
        )
        self._ensure_columns(
            conn,
            "planned_orders",
            {
                "pending_order_id": "text not null default ''",
                "order_intent_id": "text not null default ''",
                "run_id": "text not null default ''",
                "trade_date": "text not null default ''",
                "symbol": "text not null default ''",
                "side": "text not null default ''",
                "volume": "integer not null default 0",
                "status": "text not null default 'PLANNED'",
                "trigger_price": "real not null default 0",
                "lower_price": "real not null default 0",
                "upper_price": "real not null default 0",
                "stop_price": "real not null default 0",
                "target_price": "real not null default 0",
                "invalid_price": "real not null default 0",
                "reason_code": "text not null default ''",
                "reason_text": "text not null default ''",
                "last_message": "text not null default ''",
                "quote_json": "text not null default '{}'",
                "payload_json": "text not null default '{}'",
                "created_at": "text not null default ''",
                "updated_at": "text not null default ''",
            },
        )

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {str(row["name"]) for row in conn.execute(f"pragma table_info({table})").fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"alter table {table} add column {name} {ddl}")

    def _record_buy_lot(self, conn: sqlite3.Connection, request: BrokerOrderRequest) -> None:
        lot_date = request.session_date or _utc_now()[:10]
        conn.execute(
            """
            insert into position_lots(account_id, symbol, lot_date, shares)
            values (?, ?, ?, ?)
            on conflict(account_id, symbol, lot_date) do update set
                shares = shares + excluded.shares
            """,
            (self.config.account_id, request.symbol, lot_date, int(request.volume)),
        )

    def _sellable_shares(self, conn: sqlite3.Connection, symbol: str, session_date: str) -> int:
        row = conn.execute(
            """
            select coalesce(sum(shares), 0) as shares
            from position_lots
            where account_id = ? and symbol = ? and lot_date < ?
            """,
            (self.config.account_id, symbol, session_date),
        ).fetchone()
        return int(row["shares"] or 0)

    def _consume_sell_lots(self, conn: sqlite3.Connection, request: BrokerOrderRequest) -> None:
        remaining = int(request.volume)
        if request.session_date:
            lot_rows = conn.execute(
                """
                select lot_date, shares
                from position_lots
                where account_id = ? and symbol = ? and lot_date < ? and shares > 0
                order by lot_date
                """,
                (self.config.account_id, request.symbol, request.session_date),
            ).fetchall()
        else:
            lot_rows = conn.execute(
                """
                select lot_date, shares
                from position_lots
                where account_id = ? and symbol = ? and shares > 0
                order by lot_date
                """,
                (self.config.account_id, request.symbol),
            ).fetchall()
        for row in lot_rows:
            if remaining <= 0:
                break
            used = min(remaining, int(row["shares"]))
            conn.execute(
                """
                update position_lots
                set shares = ?
                where account_id = ? and symbol = ? and lot_date = ?
                """,
                (int(row["shares"]) - used, self.config.account_id, request.symbol, row["lot_date"]),
            )
            remaining -= used

    def _has_same_day_sell(self, conn: sqlite3.Connection, symbol: str, session_date: str) -> bool:
        row = conn.execute(
            """
            select 1
            from orders
            where account_id = ? and symbol = ? and side = 'SELL' and session_date = ? and status = 'FILLED'
            limit 1
            """,
            (self.config.account_id, symbol, session_date),
        ).fetchone()
        return row is not None

    def _has_risk_sell(self, conn: sqlite3.Connection, symbol: str, session_date: str) -> bool:
        row = conn.execute(
            """
            select 1
            from orders
            where account_id = ? and symbol = ? and side = 'SELL' and session_date = ?
              and status = 'FILLED' and risk_reason_code != ''
            limit 1
            """,
            (self.config.account_id, symbol, session_date),
        ).fetchone()
        return row is not None


class GMSimBrokerAdapter:
    """Guarded GM simulation adapter.

    This scaffold mirrors the production GM contract shape while remaining
    fail-closed by default. It never imports or touches GM until submit is
    explicitly enabled and all guards pass.
    """

    name = "gm_simulation"

    def __init__(self, config: GMSimulationConfig, gm_client: GmClientProtocol | None = None) -> None:
        self.config = config
        self._gm_client = gm_client
        self._connected = False

    def submit_order(self, request: BrokerOrderRequest, preflight: BrokerPreflight | None = None) -> BrokerOrderResult:
        preflight = preflight or BrokerPreflight()
        guard = self.preflight_guard(request, preflight)
        if guard is not BrokerGuardCode.OK:
            return BrokerOrderResult(
                adapter=self.name,
                submitted=False,
                accepted=False,
                reason_code=guard,
                account_id_present=bool(self.config.account_id.strip()),
                request=request.as_payload(),
            )

        try:
            client = self._resolve_gm_client()
            self._connect_client(client)
            kwargs = self._order_volume_kwargs(request, client)
            raw_order = client.order_volume(**kwargs)
        except ImportError as exc:
            return self._blocked(request, BrokerGuardCode.GM_UNAVAILABLE, str(exc))
        except Exception as exc:  # pragma: no cover - exercised with tests via fake failures if needed
            return self._blocked(request, BrokerGuardCode.GM_SUBMIT_EXCEPTION, f"{type(exc).__name__}: {exc}")

        cl_ord_id = _first_attr(raw_order, "cl_ord_id", "clOrdId", "client_order_id")
        counter_order_id = _first_attr(raw_order, "counter_order_id", "counterOrderId", "order_id")
        order_id = cl_ord_id or counter_order_id or _first_attr(raw_order, "id")
        return BrokerOrderResult(
            adapter=self.name,
            submitted=True,
            accepted=bool(order_id),
            reason_code=BrokerGuardCode.OK,
            order_id=order_id,
            cl_ord_id=cl_ord_id,
            counter_order_id=counter_order_id,
            account_id_present=bool(self.config.account_id.strip()),
            request=request.as_payload(),
            raw_order_repr=repr(raw_order)[:240],
        )

    def preflight_guard(self, request: BrokerOrderRequest, preflight: BrokerPreflight) -> BrokerGuardCode:
        reason = request.validate()
        if reason is not BrokerGuardCode.OK:
            return reason
        if self.config.account_type.strip().upper() != "SIMULATION":
            return BrokerGuardCode.NON_SIM_ACCOUNT_TYPE
        if self.config.dry_run:
            return BrokerGuardCode.DRY_RUN
        if not self.config.enable_submit:
            return BrokerGuardCode.SUBMIT_DISABLED
        if not self.config.account_id.strip():
            return BrokerGuardCode.ACCOUNT_ID_REQUIRED
        if not os.environ.get(self.config.token_env, "").strip():
            return BrokerGuardCode.TOKEN_REQUIRED
        if self.config.require_identity_gate and not preflight.identity_pass:
            return BrokerGuardCode.IDENTITY_GATE_FAILED
        if self.config.require_data_gate and not preflight.data_gate_pass:
            return BrokerGuardCode.DATA_GATE_FAILED
        if self.config.require_reconciliation and not preflight.reconciliation_pass:
            return BrokerGuardCode.RECONCILIATION_FAILED
        return BrokerGuardCode.OK

    def _connect_client(self, client: GmClientProtocol) -> None:
        if self._connected:
            return
        token = os.environ.get(self.config.token_env, "").strip()
        set_token = getattr(client, "set_token", None)
        if callable(set_token):
            set_token(token)
        set_account_id = getattr(client, "set_account_id", None)
        if callable(set_account_id):
            set_account_id(self.config.account_id.strip())
        self._connected = True

    def _order_volume_kwargs(self, request: BrokerOrderRequest, client: GmClientProtocol) -> dict[str, Any]:
        kwargs = {
            "symbol": request.symbol,
            "volume": int(request.volume),
            "side": _gm_constant(client, "OrderSide_Buy" if request.side is BrokerSide.BUY else "OrderSide_Sell"),
            "order_type": _gm_constant(client, "OrderType_Limit"),
            "position_effect": _gm_constant(
                client,
                "PositionEffect_Open" if request.position_effect.upper() == "OPEN" else "PositionEffect_Close",
            ),
            "price": float(request.price),
            "account": self.config.account_id.strip(),
        }
        return kwargs

    def _resolve_gm_client(self) -> GmClientProtocol:
        if self._gm_client is not None:
            return self._gm_client
        try:
            from gm.api import order_volume, set_account_id, set_token
            from gm.enum import OrderSide_Buy, OrderSide_Sell, OrderType_Limit, PositionEffect_Close, PositionEffect_Open
        except Exception as exc:  # pragma: no cover - depends on optional local GM SDK
            raise ImportError("GM SDK is not available") from exc
        return SimpleNamespace(
            order_volume=order_volume,
            set_account_id=set_account_id,
            set_token=set_token,
            OrderSide_Buy=OrderSide_Buy,
            OrderSide_Sell=OrderSide_Sell,
            OrderType_Limit=OrderType_Limit,
            PositionEffect_Open=PositionEffect_Open,
            PositionEffect_Close=PositionEffect_Close,
        )

    def _blocked(self, request: BrokerOrderRequest, reason: BrokerGuardCode, message: str = "") -> BrokerOrderResult:
        return BrokerOrderResult(
            adapter=self.name,
            submitted=False,
            accepted=False,
            reason_code=reason,
            account_id_present=bool(self.config.account_id.strip()),
            message=message,
            request=request.as_payload(),
        )


def _gm_constant(client: GmClientProtocol, name: str) -> Any:
    return getattr(client, name, name)


def _first_attr(obj: Any, *names: str) -> str:
    if isinstance(obj, dict):
        for name in names:
            value = obj.get(name)
            if value:
                return str(value)
        return ""
    for name in names:
        value = getattr(obj, name, None)
        if value:
            return str(value)
    return ""


def _extra_text(request: BrokerOrderRequest, key: str) -> str:
    value = request.extra.get(key, "")
    if value is None:
        return ""
    return str(value).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

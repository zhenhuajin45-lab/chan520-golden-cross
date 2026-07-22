from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.broker_adapter import BrokerOrderRequest, BrokerSide, LocalSimBrokerAdapter, LocalSimBrokerConfig
from chan520_skill.data import DataError, normalize_code, tencent_index_quote, tencent_quote
from chan520_skill.execution_policy import (
    BEAR_PILOT_ACCOUNT_ID,
    BEAR_PILOT_EXECUTION_SCOPE,
    BEAR_PILOT_POLICY_ID,
    CORE_PLAN_POLICY_ID,
)
from chan520_skill.microstructure import price_limit


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute local simulated planned orders with quote-trigger guards")
    parser.add_argument("--ledger", default="data/local_sim/broker.sqlite")
    parser.add_argument("--account-id", default="local-sim")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--max-age-minutes", type=int, default=5)
    parser.add_argument("--max-fills", type=int, default=2)
    parser.add_argument("--max-exposure-pct", type=float, default=0.15)
    parser.add_argument("--max-trigger-drawdown-pct", type=float, default=1.2)
    parser.add_argument("--max-open-drawdown-pct", type=float, default=1.0)
    parser.add_argument("--confirmation-max-minutes", type=int, default=20)
    parser.add_argument("--submit", action="store_true", help="Write accepted trigger fills into the local simulated broker")
    parser.add_argument("--ignore-time-gate", action="store_true", help="For tests/replay only; do not use for live paper execution")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id=args.account_id,
            initial_cash=args.initial_cash,
            ledger_path=args.ledger,
        )
    )
    adapter.initialize_account()
    now = datetime.now(SHANGHAI_TZ)
    payload = run_trigger_cycle(
        adapter=adapter,
        ledger=Path(args.ledger),
        account_id=args.account_id,
        trade_date=args.trade_date,
        now=now,
        max_age_minutes=args.max_age_minutes,
        max_fills=args.max_fills,
        max_exposure_pct=args.max_exposure_pct,
        max_trigger_drawdown_pct=args.max_trigger_drawdown_pct,
        submit=args.submit,
        ignore_time_gate=args.ignore_time_gate,
        max_open_drawdown_pct=args.max_open_drawdown_pct,
        confirmation_max_minutes=args.confirmation_max_minutes,
    )
    output = Path(args.output) if args.output else default_audit_path(args.trade_date, now)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary(payload), ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if not payload["blocking_errors"] else 2


def run_trigger_cycle(
    *,
    adapter: LocalSimBrokerAdapter,
    ledger: Path,
    account_id: str,
    trade_date: str,
    now: datetime,
    max_age_minutes: int,
    max_fills: int,
    max_exposure_pct: float,
    max_trigger_drawdown_pct: float,
    submit: bool,
    ignore_time_gate: bool = False,
    max_open_drawdown_pct: float = 1.0,
    confirmation_max_minutes: int = 20,
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plans = load_watch_plans(ledger, account_id, trade_date)
    account = adapter.account_snapshot()
    time_gate_ok = ignore_time_gate or is_continuous_auction(now.time())
    if not plans:
        return {
            "schema_version": "chan520_local_sim_trigger_audit_v0",
            "generated_at": now.isoformat(timespec="seconds"),
            "trade_date": trade_date,
            "account_id": account_id,
            "submit": submit,
            "time_gate_ok": time_gate_ok,
            "max_age_minutes": max_age_minutes,
            "max_fills": max_fills,
            "max_exposure_pct": max_exposure_pct,
            "max_trigger_drawdown_pct": max_trigger_drawdown_pct,
            "max_open_drawdown_pct": max_open_drawdown_pct,
            "confirmation_max_minutes": confirmation_max_minutes,
            "market_context": {"status": "NOT_REQUIRED", "indices": {}},
            "equity": float(account["cash"]),
            "used_exposure": None,
            "active_risk_exit_count": 0,
            "plan_count": 0,
            "results": [],
            "blocking_errors": [],
        }
    marked = mark_account_positions(account, now=now, max_age_minutes=max_age_minutes)
    equity = float(account["cash"]) + marked["market_value"]
    used_exposure = marked["market_value"]
    active_risk_exit_count = count_active_risk_exits(ledger, account_id, trade_date)
    filled_count = today_buy_count(ledger, account_id, trade_date)
    results: list[dict[str, Any]] = []
    blocking_errors: list[dict[str, str]] = []
    if marked["errors"]:
        blocking_errors.extend(marked["errors"])
    if market_context is None and time_gate_ok and plans and filled_count < max_fills:
        market_context = load_market_context(now=now, max_age_minutes=max_age_minutes)
    market_context = market_context or {"status": "NOT_REQUIRED", "indices": {}}

    for plan in sorted(plans, key=plan_rank):
        decision = evaluate_plan(
            plan,
            trade_date=trade_date,
            now=now,
            max_age_minutes=max_age_minutes,
            time_gate_ok=time_gate_ok,
            filled_count=filled_count,
            max_fills=max_fills,
            used_exposure=used_exposure,
            equity=equity,
            max_exposure_pct=max_exposure_pct,
            max_trigger_drawdown_pct=max_trigger_drawdown_pct,
            max_open_drawdown_pct=max_open_drawdown_pct,
            confirmation_max_minutes=confirmation_max_minutes,
            market_context=market_context,
            account_marks_ok=not marked["errors"],
            active_risk_exit_count=active_risk_exit_count,
            account_id=account_id,
        )
        result_row = {"planned_order_id": plan["planned_order_id"], "symbol": plan["symbol"], **decision}
        if decision["action"] == "CONFIRM" and submit:
            adapter.mark_planned_order(
                str(plan["planned_order_id"]),
                "CONFIRMED_TRIGGER",
                decision["message"],
                decision.get("quote", {}),
            )
        elif decision["action"] == "CONFIRM":
            result_row["message"] = "dry-run would mark CONFIRMED_TRIGGER; next valid cycle may submit"
        elif decision["action"] == "SUBMIT" and submit:
            broker_result = submit_plan(adapter, plan, trade_date, decision)
            adapter.mark_planned_order(
                str(plan["planned_order_id"]),
                "FILLED" if broker_result.accepted else f"REJECTED_{broker_result.reason_code.value}",
                broker_result.message,
                decision.get("quote", {}),
            )
            result_row["broker_result"] = broker_result.as_payload()
            if broker_result.accepted:
                filled_count += 1
                used_exposure += float(decision["price"]) * int(plan["volume"])
        elif decision["action"] == "SUBMIT":
            result_row["message"] = "dry-run would submit; add --submit to write local simulated fill"
        elif (
            submit
            and str(plan.get("status") or "").upper() == "CONFIRMED_TRIGGER"
            and decision["action"] == "WAIT"
            and decision["reason"] not in {"MAX_FILLS_REACHED", "NOT_IN_CONTINUOUS_AUCTION"}
        ):
            adapter.mark_planned_order(
                str(plan["planned_order_id"]),
                "WATCH_TRIGGER",
                f"confirmation reset: {decision['reason']} {decision['message']}",
                decision.get("quote", {}),
            )
            result_row["action"] = "RESET"
        results.append(result_row)

    return {
        "schema_version": "chan520_local_sim_trigger_audit_v0",
        "generated_at": now.isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "account_id": account_id,
        "submit": submit,
        "time_gate_ok": time_gate_ok,
        "max_age_minutes": max_age_minutes,
        "max_fills": max_fills,
        "max_exposure_pct": max_exposure_pct,
        "max_trigger_drawdown_pct": max_trigger_drawdown_pct,
        "max_open_drawdown_pct": max_open_drawdown_pct,
        "confirmation_max_minutes": confirmation_max_minutes,
        "market_context": market_context,
        "equity": equity,
        "used_exposure": used_exposure,
        "active_risk_exit_count": active_risk_exit_count,
        "plan_count": len(plans),
        "results": results,
        "blocking_errors": blocking_errors,
    }


def load_watch_plans(ledger: Path, account_id: str, trade_date: str) -> list[dict[str, Any]]:
    with sqlite3.connect(ledger) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select planned_order_id, pending_order_id, order_intent_id, run_id,
                   trade_date, symbol, side, volume, status, trigger_price,
                   lower_price, upper_price, stop_price, target_price,
                   invalid_price, reason_code, reason_text, quote_json,
                   payload_json, created_at, updated_at
            from planned_orders
            where account_id = ?
              and trade_date = ?
              and upper(side) = 'BUY'
              and status in ('WATCH_TRIGGER', 'CONFIRMED_TRIGGER', 'PLANNED', 'REJECTED_STALE_QUOTE', 'REJECTED_NOT_TRIGGERED')
            order by order_intent_id, planned_order_id
            """,
            (account_id, trade_date),
        ).fetchall()
    return [row_to_plan(row) for row in rows]


def row_to_plan(row: sqlite3.Row) -> dict[str, Any]:
    plan = {key: row[key] for key in row.keys()}
    plan["payload"] = parse_json(plan.get("payload_json"), {})
    plan["confirmation_quote"] = parse_json(plan.get("quote_json"), {})
    return plan


def count_active_risk_exits(ledger: Path, account_id: str, trade_date: str) -> int:
    if not ledger.exists():
        return 0
    with sqlite3.connect(ledger) as conn:
        row = conn.execute(
            """
            select count(*)
            from planned_orders
            where account_id = ? and upper(side) = 'SELL' and trade_date <= ?
              and status in ('RISK_CANDIDATE', 'RISK_CONFIRMED')
            """,
            (account_id, trade_date),
        ).fetchone()
    return int(row[0] or 0)


def evaluate_plan(
    plan: dict[str, Any],
    *,
    trade_date: str,
    now: datetime,
    max_age_minutes: int,
    time_gate_ok: bool,
    filled_count: int,
    max_fills: int,
    used_exposure: float,
    equity: float,
    max_exposure_pct: float,
    max_trigger_drawdown_pct: float,
    max_open_drawdown_pct: float,
    confirmation_max_minutes: int,
    market_context: dict[str, Any],
    account_marks_ok: bool,
    active_risk_exit_count: int,
    account_id: str | None = None,
    raw_quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = str(plan.get("symbol") or "")
    code = normalize_code(symbol)
    if not time_gate_ok:
        return reject("NOT_IN_CONTINUOUS_AUCTION", "not in 09:30-11:30 or 13:00-14:57 continuous auction window")
    if filled_count >= max_fills:
        return reject("MAX_FILLS_REACHED", f"today filled count {filled_count} >= {max_fills}")
    if not account_marks_ok:
        return reject("ACCOUNT_MARK_UNAVAILABLE", "existing positions could not be marked; total exposure is unknown")
    if active_risk_exit_count > 0:
        return reject(
            "ACCOUNT_RISK_EXIT_PENDING",
            f"{active_risk_exit_count} unresolved risk exit plan(s); new entries remain blocked",
        )
    payload = plan.get("payload") if isinstance(plan.get("payload"), dict) else {}
    policy_id = str(payload.get("local_sim_execution_policy_id") or "")
    if policy_id not in {CORE_PLAN_POLICY_ID, BEAR_PILOT_POLICY_ID}:
        return reject("UNAPPROVED_EXECUTION_POLICY", "plan was not generated by the approved daily core-plan policy")
    is_bear_pilot = policy_id == BEAR_PILOT_POLICY_ID
    if is_bear_pilot and account_id is not None and account_id != BEAR_PILOT_ACCOUNT_ID:
        return reject("RESEARCH_PILOT_ACCOUNT_MISMATCH", "bear pilot plan cannot execute in the core or another account")
    if account_id == BEAR_PILOT_ACCOUNT_ID and not is_bear_pilot:
        return reject("RESEARCH_PILOT_POLICY_REQUIRED", "bear pilot account accepts only the isolated bear pilot policy")
    if is_bear_pilot and not (
        payload.get("research_pilot") is True
        and payload.get("core_account_affected") is False
        and payload.get("gm_submit_enabled") is False
        and str(payload.get("execution_scope") or "") == BEAR_PILOT_EXECUTION_SCOPE
        and str(payload.get("market_regime") or "").upper() == "BEAR"
    ):
        return reject("INVALID_RESEARCH_PILOT_SCOPE", "bear pilot must be isolated, local-only, and precomputed in BEAR regime")
    if not is_bear_pilot and str(payload.get("market_regime") or "").upper() in {"BEAR", "UNKNOWN", "DOWN"}:
        return reject("PLAN_MARKET_REGIME_BLOCKED", f"plan regime={payload.get('market_regime')}")
    if market_context.get("status") != "OK":
        return reject("MARKET_CONTEXT_UNAVAILABLE", str(market_context.get("message") or "intraday index quotes unavailable"))
    try:
        quote = normalize_quote(raw_quote if raw_quote is not None else tencent_quote(code), now=now, max_age_minutes=max_age_minutes)
    except (DataError, ValueError, TimeoutError, OSError) as exc:
        return reject("QUOTE_ERROR", f"{type(exc).__name__}: {exc}")
    price = float(quote["price"])
    if quote["quote_date"] != trade_date:
        return reject("STALE_QUOTE_DATE", f"quote date {quote['quote_date']} != trade date {trade_date}", quote=quote)
    if quote["age_minutes"] is None or quote["age_minutes"] > max_age_minutes:
        return reject("STALE_QUOTE_TIME", f"quote age {quote['age_minutes']} min > {max_age_minutes}", quote=quote)
    if quote["is_suspended"]:
        return reject("SUSPENDED_OR_NO_QUOTE", "quote has non-positive price or previous close", quote=quote)
    if quote["limit_up"] or quote["limit_down"]:
        return reject("LIMIT_PRICE_BLOCKED", "quote is near limit up/down", quote=quote)
    market_guard = evaluate_market_context(code, quote, market_context)
    if market_guard is not None:
        return reject(market_guard[0], market_guard[1], quote=quote)
    metrics = signal_metrics(plan, code)
    if not metrics:
        return reject("MISSING_SIGNAL_METRICS", "missing MA5/MA20 signal metrics; fail closed", quote=quote)
    ma5 = safe_float(metrics.get("ma5"))
    ma20 = safe_float(metrics.get("ma20"))
    if ma20 > 0 and price < ma20:
        return reject("BELOW_MA20_RISK", f"price {price:.2f} < MA20 {ma20:.2f}; trend support broken", quote=quote)
    if ma5 > 0 and price < ma5:
        return reject("BELOW_MA5_CONFIRMATION", f"price {price:.2f} < MA5 {ma5:.2f}; waiting for 5-day reclaim", quote=quote)
    prev_close = safe_float(quote.get("prev_close"))
    if prev_close > 0:
        intraday_pct = (price / prev_close - 1) * 100
        if intraday_pct < -abs(max_trigger_drawdown_pct):
            return reject(
                "INTRADAY_WEAKNESS_BLOCKED",
                f"price change {intraday_pct:.2f}% < -{abs(max_trigger_drawdown_pct):.2f}%; weak trigger blocked",
                quote=quote,
            )
    open_price = safe_float(quote.get("open"))
    if open_price > 0:
        from_open_pct = (price / open_price - 1) * 100
        if from_open_pct < -abs(max_open_drawdown_pct):
            return reject(
                "OPENING_WEAKNESS_BLOCKED",
                f"price vs open {from_open_pct:.2f}% < -{abs(max_open_drawdown_pct):.2f}%; opening support failed",
                quote=quote,
            )
    lower = safe_float(plan.get("lower_price"))
    upper = safe_float(plan.get("upper_price") or plan.get("trigger_price"))
    trigger = safe_float(plan.get("trigger_price"))
    invalid = safe_float(plan.get("invalid_price"))
    stop = safe_float(plan.get("stop_price"))
    if not (stop > 0 and stop < lower <= trigger <= upper < invalid):
        return reject(
            "INVALID_PLAN_GEOMETRY",
            f"requires stop < lower <= trigger <= upper < invalid, got {stop:.2f} < {lower:.2f} <= {trigger:.2f} <= {upper:.2f} < {invalid:.2f}",
            quote=quote,
        )
    if lower > 0 and price < lower:
        return reject("BELOW_TRIGGER_ZONE", f"price {price:.2f} < lower {lower:.2f}", quote=quote)
    if upper > 0 and price > upper:
        return reject("ABOVE_TRIGGER_ZONE", f"price {price:.2f} > upper {upper:.2f}", quote=quote)
    if invalid > 0 and price > invalid:
        return reject("CHASE_INVALID_PRICE", f"price {price:.2f} > invalid {invalid:.2f}", quote=quote)
    order_gross = price * int(float(plan.get("volume") or 0))
    if equity > 0 and used_exposure + order_gross > equity * max_exposure_pct:
        return reject(
            "EXPOSURE_CAP_REACHED",
            f"gross {used_exposure + order_gross:.2f} > cap {equity * max_exposure_pct:.2f}",
            quote=quote,
        )
    if str(plan.get("status") or "").upper() != "CONFIRMED_TRIGGER":
        return {
            "action": "CONFIRM",
            "reason": "TRIGGER_CONFIRMED",
            "message": f"first-stage confirmation: price {price:.2f} within {lower:.2f}-{upper:.2f}, above MA5/MA20",
            "price": price,
            "quote": quote,
            "metrics": metrics,
        }
    confirmation = plan.get("confirmation_quote") if isinstance(plan.get("confirmation_quote"), dict) else {}
    confirmation_time = parse_quote_time(confirmation.get("quote_time"))
    confirmation_price = safe_float(confirmation.get("price"))
    if confirmation_time is None or confirmation_price <= 0:
        return reject("CONFIRMATION_EVIDENCE_MISSING", "confirmed plan has no valid first-stage quote", quote=quote)
    confirmation_age = (now - confirmation_time).total_seconds() / 60
    if confirmation_age < 2:
        return reject("CONFIRMATION_MIN_WAIT", f"confirmation age {confirmation_age:.1f} min < 2 min", quote=quote)
    if confirmation_age > confirmation_max_minutes:
        return reject(
            "CONFIRMATION_EXPIRED",
            f"confirmation age {confirmation_age:.1f} min > {confirmation_max_minutes} min",
            quote=quote,
        )
    if price < confirmation_price * 0.995:
        return reject(
            "CONFIRMATION_WEAKENED",
            f"price {price:.2f} fell more than 0.5% below first confirmation {confirmation_price:.2f}",
            quote=quote,
        )
    return {
        "action": "SUBMIT",
        "reason": "TRIGGER_MATCHED",
        "message": f"price {price:.2f} within {lower:.2f}-{upper:.2f}",
        "price": price,
        "quote": quote,
        "metrics": metrics,
    }


def submit_plan(adapter: LocalSimBrokerAdapter, plan: dict[str, Any], trade_date: str, decision: dict[str, Any]):
    payload = plan.get("payload") or {}
    return adapter.submit_order(
        BrokerOrderRequest(
            symbol=str(plan.get("symbol") or ""),
            side=BrokerSide.BUY,
            volume=int(float(plan.get("volume") or 0)),
            price=float(decision["price"]),
            client_order_id=str(plan.get("planned_order_id") or ""),
            order_intent_id=str(plan.get("order_intent_id") or ""),
            run_id=str(plan.get("run_id") or ""),
            session_date=trade_date,
            position_effect="OPEN",
            extra={
                "signal_name": "local_sim_bear_pilot" if payload.get("research_pilot") is True else "local_sim_trigger_executor",
                "entry_reason": str(payload.get("entry_reason") or plan.get("reason_text") or ""),
                "risk_reason": str(payload.get("exit_risk_reason") or ""),
                "risk_reason_code": str(plan.get("reason_code") or ""),
                "previous_session_date": str(payload.get("signal_date") or ""),
                "notes": json.dumps(
                    {
                        "planned_order_id": plan.get("planned_order_id"),
                        "trigger_price": plan.get("trigger_price"),
                        "lower_price": plan.get("lower_price"),
                        "upper_price": plan.get("upper_price"),
                        "stop_price": plan.get("stop_price"),
                        "target_price": plan.get("target_price"),
                        "execution_policy_id": payload.get("local_sim_execution_policy_id"),
                        "research_pilot": payload.get("research_pilot") is True,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        )
    )


def normalize_quote(raw: dict[str, Any], *, now: datetime, max_age_minutes: int) -> dict[str, Any]:
    price = safe_float(raw.get("price"))
    prev_close = safe_float(raw.get("prev_close"))
    quote_time = parse_quote_time(raw.get("datetime"))
    age_minutes = None
    if quote_time is not None:
        age_minutes = max(0.0, (now - quote_time).total_seconds() / 60)
    limit_pct = price_limit(str(raw.get("code") or ""))
    limit_up = bool(prev_close > 0 and price >= prev_close * (1 + (limit_pct - 0.3) / 100))
    limit_down = bool(prev_close > 0 and price <= prev_close * (1 - (limit_pct - 0.3) / 100))
    return {
        "code": str(raw.get("code") or ""),
        "name": str(raw.get("name") or ""),
        "price": price,
        "prev_close": prev_close,
        "open": safe_float(raw.get("open")),
        "high": safe_float(raw.get("high")),
        "low": safe_float(raw.get("low")),
        "pct_chg": safe_float(raw.get("pct_chg")),
        "quote_time": quote_time.isoformat(timespec="seconds") if quote_time else "",
        "quote_date": quote_time.date().isoformat() if quote_time else "",
        "age_minutes": age_minutes,
        "max_age_minutes": max_age_minutes,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "is_suspended": price <= 0 or prev_close <= 0,
    }


def mark_account_positions(account: dict[str, Any], *, now: datetime, max_age_minutes: int) -> dict[str, Any]:
    market_value = 0.0
    errors: list[dict[str, str]] = []
    for position in account.get("positions", []):
        symbol = str(position.get("symbol") or "")
        try:
            quote = normalize_quote(tencent_quote(normalize_code(symbol)), now=now, max_age_minutes=max_age_minutes)
            if quote["quote_date"] != now.date().isoformat() or quote["age_minutes"] is None or quote["age_minutes"] > max_age_minutes:
                raise ValueError("stale account mark")
            market_value += safe_float(quote.get("price")) * int(position.get("shares", 0) or 0)
        except (DataError, ValueError, TimeoutError, OSError) as exc:
            errors.append({"code": "ACCOUNT_MARK_UNAVAILABLE", "symbol": symbol, "message": f"{type(exc).__name__}: {exc}"})
    return {"market_value": market_value, "errors": errors}


def load_market_context(*, now: datetime, max_age_minutes: int) -> dict[str, Any]:
    indices: dict[str, dict[str, Any]] = {}
    errors = []
    for symbol in ("000001", "399001", "399006", "000688"):
        try:
            quote = normalize_quote(tencent_index_quote(symbol), now=now, max_age_minutes=max_age_minutes)
            if quote["quote_date"] != now.date().isoformat() or quote["age_minutes"] is None or quote["age_minutes"] > max_age_minutes:
                raise ValueError("stale index quote")
            indices[symbol] = quote
        except (DataError, ValueError, TimeoutError, OSError) as exc:
            errors.append(f"{symbol}:{type(exc).__name__}")
    return {
        "status": "OK" if not errors else "UNAVAILABLE",
        "indices": indices,
        "message": ",".join(errors),
    }


def evaluate_market_context(code: str, quote: dict[str, Any], context: dict[str, Any]) -> tuple[str, str] | None:
    index_symbol = "000688" if code.startswith(("688", "689")) else "399006" if code.startswith("3") else "000001" if code.startswith(("5", "6", "9")) else "399001"
    index_quote = (context.get("indices") or {}).get(index_symbol, {})
    index_pct = safe_float(index_quote.get("pct_chg"))
    stock_pct = safe_float(quote.get("pct_chg"))
    shock_indices = [
        symbol
        for symbol, item in (context.get("indices") or {}).items()
        if safe_float(item.get("pct_chg")) <= -1.5
    ]
    if len(shock_indices) >= 2:
        return (
            "BROAD_MARKET_SHOCK_BLOCKED",
            f"{len(shock_indices)} major indices are down at least 1.50%: {','.join(sorted(shock_indices))}",
        )
    if index_pct <= -2.5:
        return "BOARD_RISK_BLOCKED", f"board index {index_symbol} change {index_pct:.2f}% <= -2.50%"
    if stock_pct - index_pct < -2.0:
        return "RELATIVE_WEAKNESS_BLOCKED", f"stock {stock_pct:.2f}% underperforms board {index_pct:.2f}% by more than 2.0%"
    return None


def signal_metrics(plan: dict[str, Any], code: str) -> dict[str, Any]:
    payload = plan.get("payload") if isinstance(plan.get("payload"), dict) else {}
    out: dict[str, Any] = {}
    for key in ("ma5", "ma10", "ma20", "ma60", "score", "volume_ratio", "rsi14", "signal_date"):
        value = plan.get(key)
        if value in (None, ""):
            value = payload.get(key)
        if value not in (None, ""):
            out[key] = value
    if all(safe_float(out.get(key)) > 0 for key in ("ma5", "ma20")):
        return out
    signal_date = str(payload.get("signal_date") or plan.get("signal_date") or "").strip()
    if not signal_date:
        return out
    row = scan_row(signal_date, code)
    if not row:
        return out
    for key in ("ma5", "ma10", "ma20", "ma60", "score", "volume_ratio", "rsi14", "pct_chg", "verdict", "main_signal"):
        if key in row and row[key] not in (None, ""):
            out[key] = row[key]
    out["signal_date"] = signal_date
    return out


def scan_row(signal_date: str, code: str) -> dict[str, str]:
    path = ROOT / "reports" / f"scan_{signal_date}" / f"market_scan_{signal_date}.csv"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if str(row.get("code") or "") == code:
                    return row
    except OSError:
        return {}
    return {}


def parse_quote_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if len(text) >= 14 and text[:14].isdigit():
        return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI_TZ)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=SHANGHAI_TZ)


def is_continuous_auction(value: time) -> bool:
    return time(9, 30) <= value <= time(11, 30) or time(13, 0) <= value <= time(14, 57)


def today_buy_gross(ledger: Path, account_id: str, trade_date: str) -> float:
    with sqlite3.connect(ledger) as conn:
        row = conn.execute(
            """
            select coalesce(sum(f.gross), 0) as gross
            from fills f
            join orders o on o.account_id = f.account_id and o.order_id = f.order_id
            where f.account_id = ? and o.session_date = ? and upper(f.side) = 'BUY'
            """,
            (account_id, trade_date),
        ).fetchone()
    return float(row[0] if row else 0.0)


def today_buy_count(ledger: Path, account_id: str, trade_date: str) -> int:
    with sqlite3.connect(ledger) as conn:
        row = conn.execute(
            """
            select count(*) as n
            from fills f
            join orders o on o.account_id = f.account_id and o.order_id = f.order_id
            where f.account_id = ? and o.session_date = ? and upper(f.side) = 'BUY'
            """,
            (account_id, trade_date),
        ).fetchone()
    return int(row[0] if row else 0)


def reject(reason: str, message: str, *, quote: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"action": "WAIT", "reason": reason, "message": message, "quote": quote or {}}


def plan_rank(plan: dict[str, Any]) -> tuple[int, str, str]:
    payload = plan.get("payload") if isinstance(plan.get("payload"), dict) else {}
    priority = safe_int(plan.get("execution_priority") or payload.get("execution_priority"), 1_000_000)
    return (priority, str(plan.get("order_intent_id") or ""), str(plan.get("planned_order_id") or ""))


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_json(value: Any, default: Any) -> Any:
    try:
        if not value:
            return default
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def default_audit_path(trade_date: str, now: datetime) -> Path:
    return ROOT / "reports" / "local_sim_trigger" / trade_date.replace("-", "") / f"{now.strftime('%H%M%S')}.json"


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    submitted = 0
    confirmed = 0
    for row in payload.get("results", []):
        reasons[str(row.get("reason") or "")] = reasons.get(str(row.get("reason") or ""), 0) + 1
        if row.get("action") == "CONFIRM":
            confirmed += 1
        broker = row.get("broker_result") or {}
        if broker.get("accepted"):
            submitted += 1
    return {
        "trade_date": payload.get("trade_date"),
        "submit": payload.get("submit"),
        "time_gate_ok": payload.get("time_gate_ok"),
        "plan_count": payload.get("plan_count"),
        "confirmed_count": confirmed,
        "accepted_count": submitted,
        "reasons": reasons,
    }


if __name__ == "__main__":
    raise SystemExit(main())

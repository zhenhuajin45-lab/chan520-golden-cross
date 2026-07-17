from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.broker_adapter import BrokerOrderRequest, BrokerSide, LocalSimBrokerAdapter, LocalSimBrokerConfig  # noqa: E402
from chan520_skill.data import DataError, normalize_code, tencent_quote  # noqa: E402
from scripts.execute_local_sim_triggers import is_continuous_auction, normalize_quote, parse_quote_time  # noqa: E402
from scripts.local_sim_risk_scan import latest_scan_row  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute confirmed T+1 local simulated risk exits")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--ledger", default="data/local_sim/broker.sqlite")
    parser.add_argument("--account-id", default="local-sim")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--max-age-minutes", type=int, default=5)
    parser.add_argument("--confirmation-max-minutes", type=int, default=20)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--ignore-time-gate", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id=args.account_id, initial_cash=args.initial_cash, ledger_path=args.ledger)
    )
    now = datetime.now(TZ)
    payload = run_risk_exit_cycle(
        adapter=adapter,
        ledger=Path(args.ledger),
        account_id=args.account_id,
        trade_date=args.trade_date,
        now=now,
        max_age_minutes=args.max_age_minutes,
        confirmation_max_minutes=args.confirmation_max_minutes,
        submit=args.submit,
        ignore_time_gate=args.ignore_time_gate,
    )
    output = Path(args.output) if args.output else ROOT / "reports" / "local_sim_risk_exit" / args.trade_date.replace("-", "") / f"{now:%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary(payload), ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if not payload["blocking_errors"] else 2


def run_risk_exit_cycle(
    *,
    adapter: LocalSimBrokerAdapter,
    ledger: Path,
    account_id: str,
    trade_date: str,
    now: datetime,
    max_age_minutes: int,
    confirmation_max_minutes: int,
    submit: bool,
    ignore_time_gate: bool = False,
) -> dict[str, Any]:
    plans = load_risk_plans(ledger, account_id, trade_date)
    positions = {str(row["symbol"]): row for row in adapter.account_snapshot().get("positions", [])}
    time_gate_ok = ignore_time_gate or is_continuous_auction(now.time())
    results = []
    for plan in plans:
        symbol = str(plan.get("symbol") or "")
        position = positions.get(symbol)
        if not time_gate_ok:
            decision = wait("NOT_IN_CONTINUOUS_AUCTION", "risk exits only run in continuous auction")
        elif position is None or int(position.get("shares", 0) or 0) <= 0:
            decision = {"action": "RESOLVE", "reason": "NO_POSITION", "message": "position already closed", "quote": {}}
        elif sellable_shares(ledger, account_id, symbol, trade_date) <= 0:
            decision = wait("T_PLUS_ONE_WAIT", "position is not sellable in this session")
        else:
            decision = evaluate_risk_plan(
                plan,
                position,
                trade_date=trade_date,
                now=now,
                max_age_minutes=max_age_minutes,
                confirmation_max_minutes=confirmation_max_minutes,
            )
        result = {"planned_order_id": plan.get("planned_order_id"), "symbol": symbol, **decision}
        if submit and decision["action"] == "CONFIRM":
            adapter.mark_planned_order(symbol_plan_id(plan), "RISK_CONFIRMED", decision["message"], decision.get("quote", {}))
        elif submit and decision["action"] == "RESOLVE":
            adapter.mark_planned_order(symbol_plan_id(plan), "RESOLVED_RISK", decision["message"], decision.get("quote", {}))
        elif submit and decision["action"] == "SELL":
            broker_result = submit_risk_exit(adapter, plan, position, trade_date, decision)
            adapter.mark_planned_order(
                symbol_plan_id(plan),
                "FILLED" if broker_result.accepted else f"REJECTED_{broker_result.reason_code.value}",
                broker_result.message,
                decision.get("quote", {}),
            )
            result["broker_result"] = broker_result.as_payload()
        results.append(result)
    return {
        "schema_version": "chan520_local_sim_risk_exit_v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "submit": submit,
        "time_gate_ok": time_gate_ok,
        "plan_count": len(plans),
        "results": results,
        "blocking_errors": [],
    }


def load_risk_plans(ledger: Path, account_id: str, trade_date: str) -> list[dict[str, Any]]:
    if not ledger.exists():
        return []
    with sqlite3.connect(ledger) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select planned_order_id, trade_date, symbol, side, volume, status,
                   trigger_price, stop_price, reason_code, reason_text,
                   quote_json, payload_json, created_at, updated_at
            from planned_orders
            where account_id = ? and upper(side) = 'SELL' and trade_date <= ?
              and status in ('RISK_CANDIDATE', 'RISK_CONFIRMED')
            order by trade_date, created_at, planned_order_id
            """,
            (account_id, trade_date),
        ).fetchall()
    out = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        item["payload"] = parse_json(item.get("payload_json"), {})
        item["confirmation_quote"] = parse_json(item.get("quote_json"), {})
        out.append(item)
    return out


def evaluate_risk_plan(
    plan: dict[str, Any],
    position: dict[str, Any],
    *,
    trade_date: str,
    now: datetime,
    max_age_minutes: int,
    confirmation_max_minutes: int,
) -> dict[str, Any]:
    code = normalize_code(str(plan.get("symbol") or ""))
    try:
        quote = normalize_quote(tencent_quote(code), now=now, max_age_minutes=max_age_minutes)
    except (DataError, ValueError, TimeoutError, OSError) as exc:
        return wait("QUOTE_ERROR", f"{type(exc).__name__}: {exc}")
    if quote["quote_date"] != trade_date or quote["age_minutes"] is None or quote["age_minutes"] > max_age_minutes:
        return wait("STALE_QUOTE", "risk exit quote date/time is stale", quote)
    if quote["is_suspended"]:
        return wait("SUSPENDED_OR_NO_QUOTE", "risk exit cannot be priced", quote)
    if quote["limit_down"]:
        return wait("LIMIT_DOWN_BLOCKED", "sell is blocked near limit down", quote)
    active_codes, details = active_risk_reasons(plan, position, quote, trade_date)
    if not active_codes:
        return {"action": "RESOLVE", "reason": "RISK_RECOVERED", "message": "risk conditions no longer persist", "quote": quote}
    message = "；".join(details)
    if "hard_stop_loss" in active_codes:
        return {
            "action": "SELL",
            "reason": "hard_stop_loss",
            "reason_codes": active_codes,
            "message": f"硬止损快速通道：{message}",
            "quote": quote,
            "price": quote["price"],
            "risk_speed": "FAST",
        }
    if str(plan.get("status") or "").upper() != "RISK_CONFIRMED":
        return {
            "action": "CONFIRM",
            "reason": active_codes[0],
            "reason_codes": active_codes,
            "message": message,
            "quote": quote,
            "price": quote["price"],
            "risk_speed": "CONFIRMED",
        }
    first = plan.get("confirmation_quote") if isinstance(plan.get("confirmation_quote"), dict) else {}
    first_time = parse_quote_time(first.get("quote_time"))
    if first_time is None:
        return wait("CONFIRMATION_EVIDENCE_MISSING", "risk confirmation quote is missing", quote)
    age = (now - first_time).total_seconds() / 60
    if age < 2:
        return wait("CONFIRMATION_MIN_WAIT", f"risk confirmation age {age:.1f} min < 2 min", quote)
    if age > confirmation_max_minutes:
        return {"action": "CONFIRM", "reason": "RISK_RECONFIRMED", "reason_codes": active_codes, "message": message, "quote": quote, "price": quote["price"]}
    return {"action": "SELL", "reason": active_codes[0], "reason_codes": active_codes, "message": message, "quote": quote, "price": quote["price"]}


def active_risk_reasons(
    plan: dict[str, Any], position: dict[str, Any], quote: dict[str, Any], trade_date: str
) -> tuple[list[str], list[str]]:
    payload = plan.get("payload") if isinstance(plan.get("payload"), dict) else {}
    raw_codes = payload.get("reason_codes") or [payload.get("reason_code") or plan.get("reason_code")]
    codes = [str(item) for item in raw_codes if str(item or "")]
    scan = latest_scan_row(str(plan.get("symbol") or ""), trade_date)
    ma5 = safe_float(payload.get("reference_ma5") or scan.get("ma5"))
    ma20 = safe_float(payload.get("reference_ma20") or scan.get("ma20"))
    avg = safe_float(position.get("average_price") or payload.get("average_price"))
    price = safe_float(quote.get("price"))
    pnl = price / avg - 1 if avg > 0 else 0.0
    hard_stop_pct = abs(safe_float(payload.get("hard_stop_pct"), 0.055))
    active: list[str] = []
    details: list[str] = []
    if "ma20_breakdown" in codes and ma20 > 0 and price < ma20:
        active.append("ma20_breakdown")
        details.append(f"现价 {price:.2f} 仍低于 MA20 {ma20:.2f}")
    if "ma5_failed_reclaim" in codes and ma5 > 0 and price < ma5 and pnl < 0:
        active.append("ma5_failed_reclaim")
        details.append(f"现价 {price:.2f} 仍低于 MA5 {ma5:.2f}")
    if "hard_stop_loss" in codes and pnl <= -hard_stop_pct:
        active.append("hard_stop_loss")
        details.append(f"浮亏 {pnl * 100:.2f}% 仍超过硬止损 {hard_stop_pct * 100:.1f}%")
    if "profit_giveback" in codes:
        peak = safe_float(payload.get("peak_unrealized_pnl_pct"))
        min_profit = safe_float(payload.get("profit_protection_min_pct"), 0.03)
        giveback_threshold = safe_float(payload.get("profit_giveback_pct"), 0.015)
        retained_ratio = safe_float(payload.get("profit_retained_ratio"), 0.65)
        giveback = peak - pnl
        if peak >= min_profit and giveback >= giveback_threshold and pnl <= peak * retained_ratio:
            active.append("profit_giveback")
            details.append(f"峰值浮盈 {peak * 100:.2f}% 回撤至 {pnl * 100:.2f}%")
    return active, details


def submit_risk_exit(
    adapter: LocalSimBrokerAdapter,
    plan: dict[str, Any],
    position: dict[str, Any],
    trade_date: str,
    decision: dict[str, Any],
):
    reason_codes = decision.get("reason_codes") or [decision.get("reason")]
    reason_text = str(decision.get("message") or plan.get("reason_text") or "")
    return adapter.submit_order(
        BrokerOrderRequest(
            symbol=str(plan.get("symbol") or ""),
            side=BrokerSide.SELL,
            volume=min(int(plan.get("volume", 0) or 0), int(position.get("shares", 0) or 0)),
            price=float(decision["price"]),
            position_effect="CLOSE",
            session_date=trade_date,
            client_order_id=f"{symbol_plan_id(plan)}:{trade_date}",
            extra={
                "signal_name": "local_sim_risk_exit_executor",
                "exit_reason": reason_text,
                "risk_reason": reason_text,
                "risk_reason_code": "|".join(str(item) for item in reason_codes if item),
                "notes": json.dumps({"planned_order_id": symbol_plan_id(plan)}, ensure_ascii=False),
            },
        )
    )


def sellable_shares(ledger: Path, account_id: str, symbol: str, trade_date: str) -> int:
    with sqlite3.connect(ledger) as conn:
        row = conn.execute(
            "select coalesce(sum(shares), 0) from position_lots where account_id = ? and symbol = ? and lot_date < ?",
            (account_id, symbol, trade_date),
        ).fetchone()
    return int(row[0] if row else 0)


def wait(reason: str, message: str, quote: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"action": "WAIT", "reason": reason, "message": message, "quote": quote or {}}


def symbol_plan_id(plan: dict[str, Any]) -> str:
    return str(plan.get("planned_order_id") or "")


def parse_json(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value)) if value else default
    except json.JSONDecodeError:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    for row in payload.get("results", []):
        key = str(row.get("reason") or "UNKNOWN")
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "trade_date": payload.get("trade_date"),
        "submit": payload.get("submit"),
        "plan_count": payload.get("plan_count"),
        "reasons": reasons,
    }


if __name__ == "__main__":
    raise SystemExit(main())

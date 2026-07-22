from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.broker_adapter import LocalSimBrokerAdapter, LocalSimBrokerConfig
from chan520_skill.data import DataError, normalize_code, tencent_quote
from chan520_skill.microstructure import price_limit


DEFAULT_OUTPUT = Path("web_dashboard/data/local_sim/latest_account.json")
DEFAULT_QUOTE_CACHE = Path("data/local_sim/quote_cache.json")
DEFAULT_RISK_STATE = Path("data/local_sim/risk_state.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export local simulated broker ledger for web dashboard")
    parser.add_argument("--ledger", default="data/local_sim/broker.sqlite")
    parser.add_argument("--account-id", default="local-sim")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--mark-quotes", action="store_true")
    parser.add_argument("--quote-max-age-minutes", type=int, default=20)
    parser.add_argument("--quote-cache", default=str(DEFAULT_QUOTE_CACHE))
    parser.add_argument("--risk-state", default=str(DEFAULT_RISK_STATE))
    args = parser.parse_args()

    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id=args.account_id,
            initial_cash=args.initial_cash,
            ledger_path=args.ledger,
        )
    )
    adapter.initialize_account()
    quote_cache_path = Path(args.quote_cache)
    quote_cache = read_json(quote_cache_path, {"quotes": {}})
    payload = build_payload(
        Path(args.ledger),
        args.account_id,
        args.trade_date,
        mark_quotes=args.mark_quotes,
        quote_max_age_minutes=args.quote_max_age_minutes,
        quote_cache=quote_cache,
        risk_state=read_json(Path(args.risk_state), {"positions": {}}),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if args.mark_quotes:
        quote_cache_path.parent.mkdir(parents=True, exist_ok=True)
        quote_cache_path.write_text(json.dumps(quote_cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"dashboard data exported {output}", flush=True)
    return 0


def build_payload(
    ledger: Path,
    account_id: str,
    trade_date: str = "",
    *,
    mark_quotes: bool = False,
    quote_max_age_minutes: int = 20,
    quote_cache: dict[str, Any] | None = None,
    risk_state: dict[str, Any] | None = None,
) -> dict:
    with sqlite3.connect(ledger) as conn:
        conn.row_factory = sqlite3.Row
        account = conn.execute("select * from accounts where account_id = ?", (account_id,)).fetchone()
        positions = rows(
            conn.execute(
                """
                select symbol, shares, average_price, updated_at
                from positions
                where account_id = ? and shares != 0
                order by symbol
                """,
                (account_id,),
            ).fetchall()
        )
        orders = rows(
            conn.execute(
                """
                select order_id, client_order_id, symbol, side, volume, price, status,
                       order_intent_id, run_id, session_date, signal_name, entry_reason,
                       exit_reason, risk_reason, risk_reason_code, notes, created_at
                from orders
                where account_id = ?
                order by created_at desc, order_id desc
                limit 500
                """,
                (account_id,),
            ).fetchall()
        )
        fills = rows(
            conn.execute(
                """
                select f.fill_id, f.order_id, f.symbol, f.side, f.volume, f.price, f.gross,
                       f.commission, f.stamp_duty, f.transfer_fee, f.cost_policy_id, f.created_at,
                       coalesce(o.client_order_id, '') as client_order_id,
                       coalesce(o.order_intent_id, '') as order_intent_id,
                       coalesce(o.run_id, '') as run_id,
                       coalesce(o.session_date, '') as session_date,
                       coalesce(o.signal_name, '') as signal_name,
                       coalesce(o.entry_reason, '') as entry_reason,
                       coalesce(o.exit_reason, '') as exit_reason,
                       coalesce(o.risk_reason, '') as risk_reason,
                       coalesce(o.risk_reason_code, '') as risk_reason_code,
                       coalesce(o.notes, '') as notes
                from fills f
                left join orders o on o.account_id = f.account_id and o.order_id = f.order_id
                where f.account_id = ?
                order by f.created_at desc, f.fill_id desc
                limit 500
                """,
                (account_id,),
            ).fetchall()
        )
        planned_orders = rows(
            conn.execute(
                """
                select planned_order_id, pending_order_id, order_intent_id, run_id,
                       trade_date, symbol, side, volume, status, trigger_price,
                       lower_price, upper_price, stop_price, target_price,
                       invalid_price, reason_code, reason_text, last_message,
                       quote_json, payload_json, created_at, updated_at
                from planned_orders
                where account_id = ?
                order by created_at desc, planned_order_id desc
                limit 500
                """,
                (account_id,),
            ).fetchall()
        )
        sellable_rows = conn.execute(
            """
            select symbol, coalesce(sum(case when lot_date < ? then shares else 0 end), 0) as sellable_shares
            from position_lots where account_id = ? group by symbol
            """,
            (trade_date or "9999-12-31", account_id),
        ).fetchall()
        sellable_by_symbol = {str(row["symbol"]): int(row["sellable_shares"]) for row in sellable_rows}
    planned_orders = enrich_planned_orders(planned_orders)
    quote_cache = quote_cache if quote_cache is not None else {"quotes": {}}
    risk_state = risk_state if risk_state is not None else {"positions": {}}
    quote_marks = quote_marks_for_positions(positions, quote_max_age_minutes, quote_cache) if mark_quotes else {}
    stock_names = stock_names_for_rows(positions, orders, fills, planned_orders, quote_marks=quote_marks, fetch_missing=mark_quotes)
    cash = float(account["cash"])
    market_value = sum(float(item["shares"]) * mark_price(item, quote_marks) for item in positions)
    total_equity = cash + market_value
    initial_cash = float(account["initial_cash"])
    total_pnl = total_equity - initial_cash
    valuation = valuation_summary(positions, quote_marks, mark_quotes)
    fills_by_day = group_fills_by_day(fills)
    position_reasons = latest_entry_reasons_by_symbol(fills)
    selected_fills = fills_by_day.get(trade_date, []) if trade_date else fills[:100]
    selected_orders = [
        order for order in orders if not trade_date or str(order.get("session_date") or day_from_timestamp(order.get("created_at"))) == trade_date
    ][:100]
    selected_trade_date = trade_date or latest_trade_date(orders, fills)
    selected_planned_orders = [
        row for row in planned_orders if not selected_trade_date or str(row.get("trade_date") or "") == selected_trade_date
    ]
    return {
        "schema_version": "chan520_local_sim_dashboard_v0",
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "trade_date": selected_trade_date,
        "valuation_basis": valuation["basis"],
        "valuation_status": valuation["status"],
        "valuation_complete": valuation["complete"],
        "core_plan": load_core_plan(selected_trade_date),
        "readiness": load_readiness(selected_trade_date),
        "counterfactual_replay": load_counterfactual_replay(selected_trade_date),
        "account": {
            "account_id": account_id,
            "initial_cash": initial_cash,
            "cash": cash,
            "market_value": market_value,
            "total_equity": total_equity,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl / initial_cash if initial_cash else 0.0,
            "gross_exposure_pct": market_value / total_equity if total_equity else 0.0,
            "open_position_count": len(positions),
            "order_count": len(orders),
            "fill_count": len(fills),
            "valuation_status": valuation["status"],
            "valuation_complete": valuation["complete"],
        },
        "positions": [
            {
                **item,
                **stock_identity(item, stock_names),
                "shares": int(item["shares"]),
                "average_price": float(item["average_price"]),
                "sellable_shares": sellable_by_symbol.get(str(item["symbol"]), 0),
                "t_plus_one_status": "SELLABLE" if sellable_by_symbol.get(str(item["symbol"]), 0) > 0 else "T_PLUS_ONE_BLOCKED",
                **position_mark_fields(item, quote_marks),
                **position_risk_fields(item, risk_state),
                **position_reasons.get(str(item["symbol"]), {}),
            }
            for item in positions
        ],
        "orders": [{**item, **stock_identity(item, stock_names)} for item in selected_orders],
        "fills": [{**item, **stock_identity(item, stock_names)} for item in selected_fills],
        "planned_orders": [{**item, **stock_identity(item, stock_names)} for item in selected_planned_orders],
        "daily": daily_rows(fills_by_day),
    }


def rows(items: list[sqlite3.Row]) -> list[dict]:
    return [{key: item[key] for key in item.keys()} for item in items]


def group_fills_by_day(fills: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for fill in fills:
        out[fill_trade_date(fill)].append(fill)
    return dict(out)


def fill_trade_date(fill: dict) -> str:
    session_date = str(fill.get("session_date") or "").strip()
    return session_date or day_from_timestamp(fill.get("created_at"))


def latest_entry_reasons_by_symbol(fills: list[dict]) -> dict[str, dict]:
    reasons: dict[str, dict] = {}
    for fill in fills:
        if str(fill.get("side") or "").upper() != "BUY":
            continue
        symbol = str(fill.get("symbol") or "")
        if not symbol or symbol in reasons:
            continue
        reasons[symbol] = {
            "signal_name": str(fill.get("signal_name") or ""),
            "entry_reason": str(fill.get("entry_reason") or ""),
            "entry_notes": str(fill.get("notes") or ""),
            "latest_entry_at": str(fill.get("created_at") or ""),
        }
    return reasons


def quote_marks_for_positions(
    positions: list[dict], max_age_minutes: int, quote_cache: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    marks: dict[str, dict[str, Any]] = {}
    cached_quotes = quote_cache.setdefault("quotes", {})
    for row in positions:
        symbol = str(row.get("symbol") or "")
        code = symbol_code(symbol)
        try:
            quote = tencent_quote(code)
            mark = parse_quote_mark(quote, max_age_minutes=max_age_minutes)
            if mark.get("market_price", 0) > 0 and mark.get("quote_time"):
                cached_quotes[symbol] = {**mark, "cached_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")}
        except (DataError, ValueError, TimeoutError, OSError) as exc:
            cached = cached_quotes.get(symbol) if isinstance(cached_quotes.get(symbol), dict) else {}
            if safe_float(cached.get("market_price")) > 0:
                mark = {
                    **cached,
                    "quote_status": "STALE_CACHE",
                    "quote_error": type(exc).__name__,
                }
            else:
                mark = {
                    "quote_status": "UNVALUED_COST_FALLBACK",
                    "quote_error": type(exc).__name__,
                    "market_price": float(row.get("average_price", 0.0) or 0.0),
                }
        marks[symbol] = mark
    return marks


def parse_quote_mark(quote: dict[str, str], *, max_age_minutes: int) -> dict[str, Any]:
    price = safe_float(quote.get("price"))
    prev_close = safe_float(quote.get("prev_close"))
    quote_time = parse_quote_time(quote.get("datetime"))
    age_minutes = None
    stale = True
    if quote_time is not None:
        age_minutes = max(0.0, (datetime.now(ZoneInfo("Asia/Shanghai")) - quote_time).total_seconds() / 60)
        stale = age_minutes > max_age_minutes
    limit_pct = price_limit(str(quote.get("code") or ""))
    limit_up = bool(prev_close > 0 and price >= prev_close * (1 + (limit_pct - 0.3) / 100))
    limit_down = bool(prev_close > 0 and price <= prev_close * (1 - (limit_pct - 0.3) / 100))
    suspended = price <= 0 or prev_close <= 0
    status = "OK"
    if suspended:
        status = "SUSPENDED_OR_NO_QUOTE"
    elif stale:
        status = "STALE"
    return {
        "stock_name": str(quote.get("name") or ""),
        "market_price": price,
        "prev_close": prev_close,
        "quote_open": safe_float(quote.get("open")),
        "quote_high": safe_float(quote.get("high")),
        "quote_low": safe_float(quote.get("low")),
        "quote_time": quote_time.isoformat(timespec="seconds") if quote_time else "",
        "quote_age_minutes": age_minutes,
        "quote_status": status,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "is_suspended": suspended,
    }


def parse_quote_time(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if len(text) >= 14 and text[:14].isdigit():
        return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return None


def mark_price(row: dict, marks: dict[str, dict[str, Any]]) -> float:
    mark = marks.get(str(row.get("symbol") or ""), {})
    return float(mark.get("market_price") or row.get("average_price") or 0.0)


def position_mark_fields(row: dict, marks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    shares = int(row["shares"])
    avg = float(row["average_price"])
    mark = marks.get(str(row.get("symbol") or ""), {})
    market_price = float(mark.get("market_price") or avg)
    market_value = shares * market_price
    cost = shares * avg
    pnl = market_value - cost
    return {
        "market_price": market_price,
        "market_value": market_value,
        "unrealized_pnl": pnl,
        "unrealized_pnl_pct": pnl / cost if cost else 0.0,
        "quote_status": mark.get("quote_status", "COST_FALLBACK"),
        "quote_time": mark.get("quote_time", ""),
        "quote_age_minutes": mark.get("quote_age_minutes"),
        "quote_open": safe_float(mark.get("quote_open")),
        "quote_high": safe_float(mark.get("quote_high")),
        "quote_low": safe_float(mark.get("quote_low")),
        "limit_up": bool(mark.get("limit_up", False)),
        "limit_down": bool(mark.get("limit_down", False)),
        "is_suspended": bool(mark.get("is_suspended", False)),
        "quote_error": str(mark.get("quote_error") or ""),
    }


def position_risk_fields(row: dict, risk_state: dict[str, Any]) -> dict[str, Any]:
    positions = risk_state.get("positions") if isinstance(risk_state.get("positions"), dict) else {}
    symbol = str(row.get("symbol") or "")
    state = positions.get(symbol)
    if not isinstance(state, dict):
        state = positions.get(symbol_key(symbol), {})
    peak = safe_float(state.get("peak_unrealized_pnl_pct"))
    return {
        "peak_unrealized_pnl_pct": peak,
        "intraday_high_pnl_pct": safe_float(state.get("intraday_high_pnl_pct")),
        "profit_protection_armed": peak >= 0.03,
        "risk_state_updated_at": str(state.get("updated_at") or ""),
    }


def valuation_summary(positions: list[dict], marks: dict[str, dict[str, Any]], mark_quotes: bool) -> dict[str, Any]:
    if not positions:
        return {"status": "COMPLETE", "complete": True, "basis": "cash_only_no_positions"}
    if not mark_quotes:
        return {"status": "DEGRADED", "complete": False, "basis": "average_cost_no_quote_marks"}
    statuses = [str(marks.get(str(row.get("symbol") or ""), {}).get("quote_status") or "MISSING") for row in positions]
    if all(status == "OK" for status in statuses):
        return {"status": "COMPLETE", "complete": True, "basis": "realtime_quotes"}
    if all(status in {"OK", "STALE", "STALE_CACHE"} for status in statuses):
        return {"status": "STALE", "complete": True, "basis": "realtime_and_last_valid_quotes"}
    return {"status": "DEGRADED", "complete": False, "basis": "incomplete_quotes_with_cost_fallback"}


def enrich_planned_orders(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        payload = parse_json(row.get("payload_json"), {})
        quote = parse_json(row.get("quote_json"), {})
        out.append({**row, "payload": payload, "quote": quote})
    return out


def stock_names_for_rows(*groups: list[dict], quote_marks: dict[str, dict[str, Any]], fetch_missing: bool) -> dict[str, str]:
    names: dict[str, str] = {}
    symbols: set[str] = set()
    for group in groups:
        for row in group:
            symbol = str(row.get("symbol") or "")
            key = symbol_key(symbol)
            if key:
                symbols.add(symbol)
            name = row_stock_name(row)
            if key and name:
                names[key] = name
    for symbol, mark in quote_marks.items():
        key = symbol_key(symbol)
        name = str(mark.get("stock_name") or "").strip()
        if key and name:
            names[key] = name
    if fetch_missing:
        for symbol in sorted(symbols):
            key = symbol_key(symbol)
            if not key or names.get(key):
                continue
            try:
                names[key] = str(tencent_quote(key).get("name") or "").strip()
            except (DataError, ValueError, TimeoutError, OSError):
                continue
    return names


def row_stock_name(row: dict[str, Any]) -> str:
    for key in ("stock_name", "name", "display_name"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else parse_json(row.get("payload_json"), {})
    for key in ("stock_name", "name", "display_name"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    quote = row.get("quote") if isinstance(row.get("quote"), dict) else parse_json(row.get("quote_json"), {})
    return str(quote.get("name") or quote.get("stock_name") or "").strip()


def stock_identity(row: dict[str, Any], names: dict[str, str]) -> dict[str, str]:
    symbol = str(row.get("symbol") or "")
    name = names.get(symbol_key(symbol), "")
    return {
        "stock_name": name,
        "display_symbol": f"{symbol} {name}".strip(),
    }


def symbol_key(symbol: str) -> str:
    try:
        return symbol_code(symbol)
    except ValueError:
        return ""


def parse_json(value: Any, default: Any) -> Any:
    try:
        if not value:
            return default
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_core_plan(trade_date: str) -> dict[str, Any]:
    if not trade_date:
        return {}
    path = ROOT / "reports" / "local_sim_plan" / trade_date.replace("-", "") / "core_plan.json"
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return {}
    if not payload:
        return load_core_plan_failure(trade_date)
    plans = payload.get("plans") if isinstance(payload.get("plans"), list) else []
    geometry_blocked_count = sum(
        1
        for row in plans
        if isinstance(row, dict)
        and (
            row.get("geometry_valid") is False
            or "INVALID_PLAN_GEOMETRY" in (row.get("blocking_reason_codes") or [])
        )
    )
    return {
        "status": payload.get("status"),
        "policy_id": payload.get("policy_id"),
        "selection_policy": payload.get("selection_policy"),
        "signal_date": payload.get("signal_date"),
        "executable_buy_count": payload.get("executable_buy_count", 0),
        "strict_scan_count": payload.get("strict_scan_count", 0),
        "watch_scan_count": payload.get("watch_scan_count", 0),
        "scan_quality": payload.get("scan_quality") or {},
        "market_regime": payload.get("market_regime") or {},
        "supplemental_market_context": payload.get("supplemental_market_context") or {},
        "research_warnings": payload.get("research_warnings") or [],
        "research_cohorts": payload.get("research_cohorts") or {},
        "geometry_blocked_count": geometry_blocked_count,
        "t1_risk_policy": "board_floor_plus_atr_and_amplitude_v1",
    }


def load_core_plan_failure(trade_date: str) -> dict[str, Any]:
    summary_path = ROOT / "reports" / "local_sim_daily" / trade_date.replace("-", "") / "plan_summary.json"
    summary = read_json(summary_path, {})
    if not isinstance(summary, dict) or summary.get("status") != "FAIL":
        return {}
    steps = summary.get("steps") if isinstance(summary.get("steps"), list) else []
    failed = next((row for row in steps if isinstance(row, dict) and int(row.get("returncode") or 0) != 0), {})
    reason = str(failed.get("stderr_tail") or failed.get("stdout_tail") or "core plan generation failed").strip()
    if len(reason) > 500:
        reason = reason[-500:]
    return {
        "status": "GENERATION_FAILED",
        "policy_id": None,
        "selection_policy": None,
        "signal_date": None,
        "executable_buy_count": 0,
        "strict_scan_count": 0,
        "watch_scan_count": 0,
        "scan_quality": {},
        "market_regime": {},
        "supplemental_market_context": {},
        "research_warnings": [],
        "research_cohorts": {},
        "geometry_blocked_count": 0,
        "t1_risk_policy": "board_floor_plus_atr_and_amplitude_v1",
        "failure_step": failed.get("name") or "generate_core_plan",
        "failure_reason": reason,
    }


def load_counterfactual_replay(trade_date: str) -> dict[str, Any]:
    if not trade_date:
        return {}
    path = ROOT / "reports" / "local_sim_counterfactual" / trade_date.replace("-", "") / "watch_only_replay.json"
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return {}
    return {
        "status": payload.get("status"),
        "policy_id": payload.get("policy_id"),
        "research_only": True,
        "live_execution_enabled": False,
        "candidate_count": payload.get("candidate_count", 0),
        "candidate_symbols": payload.get("candidate_symbols") or [],
        "filled_count": payload.get("filled_count", 0),
        "fills": payload.get("fills") or [],
        "net_mark_pnl": payload.get("net_mark_pnl", 0),
        "net_mark_return_on_equity": payload.get("net_mark_return_on_equity", 0),
        "replay_equity": payload.get("replay_equity", 0),
        "data_complete": payload.get("data_complete"),
        "error": payload.get("error", ""),
        "generated_at": payload.get("generated_at"),
    }


def load_readiness(trade_date: str) -> dict[str, Any]:
    if not trade_date:
        return {}
    path = ROOT / "reports" / "local_sim_readiness" / trade_date / "readiness.json"
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return {}
    return {
        "status": payload.get("status"),
        "local_sim_risk_loop_ready": payload.get("local_sim_risk_loop_ready"),
        "local_sim_buy_entry_ready": payload.get("local_sim_buy_entry_ready"),
        "risk_blocking_checks": payload.get("risk_blocking_checks") or [],
        "buy_entry_blocking_checks": payload.get("buy_entry_blocking_checks") or payload.get("blocking_checks") or [],
        "auto_open_close_kernel_ready": False,
        "shadow_readiness": False,
    }


def symbol_code(symbol: str) -> str:
    digits = "".join(ch for ch in str(symbol) if ch.isdigit())
    return normalize_code(digits[:6])


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def day_from_timestamp(value: str | None) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else ""


def latest_trade_date(orders: list[dict], fills: list[dict]) -> str:
    dates = [
        str(item.get("session_date") or day_from_timestamp(item.get("created_at")))
        for item in orders
    ]
    dates.extend(day_from_timestamp(item.get("created_at")) for item in fills)
    return max([item for item in dates if item], default="")


def daily_rows(fills_by_day: dict[str, list[dict]]) -> list[dict]:
    out = []
    for day, fills in sorted(fills_by_day.items(), reverse=True):
        buy_gross = sum(float(item["gross"]) for item in fills if item["side"] == "BUY")
        sell_gross = sum(float(item["gross"]) for item in fills if item["side"] == "SELL")
        fees = sum(
            float(item["commission"]) + float(item["stamp_duty"]) + float(item.get("transfer_fee", 0.0) or 0.0)
            for item in fills
        )
        out.append(
            {
                "trade_date": day,
                "fill_count": len(fills),
                "buy_gross": buy_gross,
                "sell_gross": sell_gross,
                "fees": fees,
                "net_cash_flow": sell_gross - buy_gross - fees,
            }
        )
    return out


if __name__ == "__main__":
    raise SystemExit(main())

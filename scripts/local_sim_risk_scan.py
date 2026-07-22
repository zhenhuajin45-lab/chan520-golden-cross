from __future__ import annotations

import argparse
import csv
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

from chan520_skill.broker_adapter import LocalSimBrokerAdapter, LocalSimBrokerConfig  # noqa: E402
from scripts.push_local_sim_feishu import write_json  # noqa: E402

DEFAULT_DATA = ROOT / "web_dashboard" / "data" / "local_sim" / "latest_account.json"
DEFAULT_STATE = ROOT / "data" / "local_sim" / "risk_state.json"
RISK_POLICY_ID = "local_sim_risk_policy_v2"


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan local sim positions for intraday risk exits")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--ledger", default="data/local_sim/broker.sqlite")
    parser.add_argument("--account-id", default="local-sim")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--hard-stop-pct", type=float, default=0.055)
    parser.add_argument("--profit-protection-min-pct", type=float, default=0.03)
    parser.add_argument("--profit-giveback-pct", type=float, default=0.015)
    parser.add_argument("--profit-retained-ratio", type=float, default=0.65)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    payload = read_json(Path(args.data), {})
    state = read_json(Path(args.state), {"positions": {}})
    audit = scan_risk(payload, state, args)
    if not args.dry_run:
        write_json(Path(args.state), state)
    audit_path = Path(args.output) if args.output else ROOT / "reports" / "local_sim_risk" / audit["trade_date"] / "risk_scan.json"
    write_json(audit_path, audit)
    print(json.dumps({"trade_date": audit["trade_date"], "candidate_count": len(audit["candidates"]), "dry_run": args.dry_run}, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if audit.get("status") != "FAIL_CLOSED" else 2


def scan_risk(payload: dict[str, Any], state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    trade_date = args.trade_date or str(payload.get("trade_date") or today())
    if payload.get("valuation_complete") is False:
        return {
            "schema_version": "chan520_local_sim_risk_scan_v2",
            "risk_policy_id": RISK_POLICY_ID,
            "generated_at": now(),
            "trade_date": trade_date,
            "dry_run": bool(args.dry_run),
            "status": "FAIL_CLOSED",
            "blocking_errors": ["VALUATION_INCOMPLETE"],
            "candidates": [],
        }
    state.setdefault("positions", {})
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id=args.account_id,
            initial_cash=args.initial_cash,
            ledger_path=args.ledger,
        )
    )
    candidates = []
    for row in payload.get("positions", []):
        symbol = str(row.get("symbol") or "")
        shares = int(float(row.get("shares", 0) or 0))
        if not symbol or shares <= 0:
            continue
        current_pnl = safe_float(row.get("unrealized_pnl_pct"))
        average_price = safe_float(row.get("average_price"))
        quote_high = safe_float(row.get("quote_high"))
        intraday_high_pnl = quote_high / average_price - 1 if quote_high > 0 and average_price > 0 else current_pnl
        prior_peak = safe_float(state["positions"].get(symbol, {}).get("peak_unrealized_pnl_pct"), current_pnl)
        peak = max(prior_peak, current_pnl, intraday_high_pnl)
        state["positions"][symbol] = {
            "peak_unrealized_pnl_pct": peak,
            "intraday_high_pnl_pct": intraday_high_pnl,
            "quote_high": quote_high,
            "updated_at": now(),
            "last_unrealized_pnl_pct": current_pnl,
        }
        decision = risk_decision(row, peak, args)
        if not decision:
            continue
        existing_plan = active_risk_plan(
            Path(args.ledger),
            args.account_id,
            trade_date,
            symbol,
        ) if not args.dry_run else None
        planned_order_id = (
            str(existing_plan.get("planned_order_id") or "")
            if existing_plan
            else f"RISK:{trade_date}:{symbol}"
        )
        existing_payload = existing_plan.get("payload", {}) if existing_plan else {}
        armed_at = str(existing_payload.get("armed_at") or existing_plan.get("created_at") or now()) if existing_plan else now()
        armed_trade_date = str(
            existing_payload.get("armed_trade_date")
            or existing_plan.get("trade_date")
            or trade_date
        ) if existing_plan else trade_date
        planned = {
            "planned_order_id": planned_order_id,
            "pending_order_id": planned_order_id,
            "trade_date": trade_date,
            "symbol": symbol,
            "side": "SELL",
            "volume": shares,
            "status": "RISK_CANDIDATE",
            "trigger_price": safe_float(row.get("market_price")),
            "stop_price": safe_float(row.get("market_price")),
            "invalid_price": safe_float(row.get("market_price")),
            "reason_code": decision["reason_code"],
            "reason_text": decision["reason"],
            "reason_codes": decision["reason_codes"],
            "risk_reason": decision["reason"],
            "risk_reason_code": decision["reason_code"],
            "local_sim_risk_policy_id": RISK_POLICY_ID,
            "reference_ma5": decision["ma5"],
            "reference_ma20": decision["ma20"],
            "average_price": average_price,
            "hard_stop_pct": abs(args.hard_stop_pct),
            "risk_speed": "FAST" if "hard_stop_loss" in decision["reason_codes"] else "CONFIRMED",
            "peak_unrealized_pnl_pct": peak,
            "intraday_high_pnl_pct": intraday_high_pnl,
            "profit_protection_min_pct": args.profit_protection_min_pct,
            "profit_giveback_pct": args.profit_giveback_pct,
            "profit_retained_ratio": args.profit_retained_ratio,
            "armed_at": armed_at,
            "armed_trade_date": armed_trade_date,
            "carried_from_prior_session": bool(existing_plan and armed_trade_date < trade_date),
            "notes": "local_sim_risk_scan",
        }
        if not args.dry_run:
            supersede_duplicate_risk_plans(
                Path(args.ledger),
                args.account_id,
                trade_date,
                symbol,
                planned["planned_order_id"],
            )
            adapter.record_planned_order(planned)
        candidates.append(planned)
    return {
        "schema_version": "chan520_local_sim_risk_scan_v2",
        "risk_policy_id": RISK_POLICY_ID,
        "generated_at": now(),
        "trade_date": trade_date,
        "dry_run": bool(args.dry_run),
        "status": "PASS",
        "blocking_errors": [],
        "candidates": candidates,
    }


def active_risk_plan(
    ledger: Path,
    account_id: str,
    trade_date: str,
    symbol: str,
) -> dict[str, Any] | None:
    if not ledger.exists():
        return None
    with sqlite3.connect(ledger) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select planned_order_id, trade_date, status, payload_json, quote_json,
                   created_at, updated_at
            from planned_orders
            where account_id = ? and symbol = ? and upper(side) = 'SELL'
              and trade_date <= ? and status in ('RISK_CANDIDATE', 'RISK_CONFIRMED')
            order by trade_date desc, updated_at desc, created_at desc
            """,
            (account_id, symbol, trade_date),
        ).fetchall()
    row = next(
        (item for item in rows if len(str(item["planned_order_id"]).split(":")) == 3),
        None,
    )
    if row is None:
        return None
    item = {key: row[key] for key in row.keys()}
    try:
        item["payload"] = json.loads(str(item.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        item["payload"] = {}
    return item


def supersede_duplicate_risk_plans(
    ledger: Path,
    account_id: str,
    trade_date: str,
    symbol: str,
    keep_plan_id: str,
) -> int:
    if not ledger.exists():
        return 0
    with sqlite3.connect(ledger) as conn:
        cursor = conn.execute(
            """
            update planned_orders
            set status = 'SUPERSEDED_RISK_PLAN',
                last_message = 'superseded by stable per-symbol risk plan',
                updated_at = ?
            where account_id = ? and trade_date <= ? and symbol = ? and upper(side) = 'SELL'
              and planned_order_id != ? and status in ('RISK_CANDIDATE', 'RISK_CONFIRMED')
            """,
            (now(), account_id, trade_date, symbol, keep_plan_id),
        )
        return int(cursor.rowcount)


def risk_decision(row: dict[str, Any], peak_pnl_pct: float, args: argparse.Namespace) -> dict[str, Any] | None:
    pnl = safe_float(row.get("unrealized_pnl_pct"))
    symbol = row.get("symbol")
    market_price = safe_float(row.get("market_price"))
    metrics = signal_metrics(row, str(args.trade_date or ""))
    ma20 = safe_float(metrics.get("ma20"))
    ma5 = safe_float(metrics.get("ma5"))
    reasons: list[tuple[str, str]] = []
    if ma20 > 0 and market_price > 0 and market_price < ma20:
        reasons.append(("ma20_breakdown", f"{symbol} 现价 {market_price:.2f} 跌破 MA20 {ma20:.2f}，520 中期趋势支撑失守"))
    if ma5 > 0 and market_price > 0 and market_price < ma5 and pnl < 0:
        reasons.append(("ma5_failed_reclaim", f"{symbol} 现价 {market_price:.2f} 跌破 MA5 {ma5:.2f}，5 日线确认失败"))
    if pnl <= -abs(args.hard_stop_pct):
        reasons.append(("hard_stop_loss", f"{symbol} 浮亏 {pnl * 100:.2f}% 触发硬止损 {abs(args.hard_stop_pct) * 100:.1f}%"))
    giveback = peak_pnl_pct - pnl
    if (
        peak_pnl_pct >= args.profit_protection_min_pct
        and giveback >= args.profit_giveback_pct
        and pnl <= peak_pnl_pct * args.profit_retained_ratio
    ):
        reasons.append(("profit_giveback", f"{symbol} 峰值浮盈 {peak_pnl_pct * 100:.2f}% 回撤至 {pnl * 100:.2f}%"))
    if not reasons:
        return None
    return {
        "reason_code": reasons[0][0],
        "reason_codes": [item[0] for item in reasons],
        "reason": "盘中风控候选：" + "；".join(item[1] for item in reasons) + f"；当前浮盈亏 {pnl * 100:.2f}%",
        "ma5": ma5,
        "ma20": ma20,
    }


def signal_metrics(row: dict[str, Any], trade_date: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("ma5", "ma10", "ma20", "ma60"):
        if row.get(key) not in (None, ""):
            out[key] = row.get(key)
    if all(safe_float(out.get(key)) > 0 for key in ("ma5", "ma20")):
        return out
    symbol = str(row.get("symbol") or "")
    scan = latest_scan_row(symbol, trade_date)
    for key in ("ma5", "ma10", "ma20", "ma60"):
        if scan.get(key) not in (None, ""):
            out[key] = scan.get(key)
    return out


def latest_scan_row(symbol: str, trade_date: str) -> dict[str, str]:
    code = "".join(ch for ch in symbol if ch.isdigit())[:6]
    if not code:
        return {}
    candidates = []
    for path in (ROOT / "reports").glob("scan_*/market_scan_*.csv"):
        date_text = path.parent.name.removeprefix("scan_")
        if trade_date and date_text >= trade_date:
            continue
        candidates.append((date_text, path))
    for _, path in sorted(candidates, reverse=True):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    if str(row.get("code") or "") == code:
                        return row
        except OSError:
            continue
    return {}


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
